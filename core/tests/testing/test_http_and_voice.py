"""Contract additions for CalDAV-style modules (MODULES §5.3, §12.1, §12.3).

* ``HttpMethod`` includes the WebDAV read methods; dry-run serves ``GET``/``PROPFIND``/
  ``REPORT`` from fixtures and records everything else.
* ``setting:<name>`` egress resolves against settings in the fake exactly as in ``HttpxClient``.
* ``HttpxClient`` sends a ``str`` body as raw content (an XML ``REPORT``, an iCalendar ``PUT``).
* An eval case's ``voice`` scores reach ``authorize`` (identity step of IDENTITY_AND_TRUST §7.2).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from beepboop.module import ToolError
from beepboop.module.types import SAFE_HTTP_METHODS
from beepboop.service_http import HttpxClient, host_allowed
from beepboop.testing.evals import run_evals_async
from beepboop.testing.fakes import FakeHttp, HttpFixture

MANIFEST = """\
id: dav
api: 1
version: 0.1.0
summary: "Test module."
scope: profile
category: productivity
egress:
  - destination: "setting:url"
    purpose: "Test."
settings:
  type: object
  properties:
    url: {type: string, format: uri, title: URL}
tools:
  - name: dav.read
    description: "Read things for the user."
    risk: personal
    untrusted_output: false
    args: {type: object, properties: {}, additionalProperties: false}
    output: {type: object, properties: {speak: {type: string}}, required: [speak]}
  - name: dav.poke
    description: "Poke a thing for the user."
    risk: act
    untrusted_output: false
    args: {type: object, properties: {}, additionalProperties: false}
    output: {type: object, properties: {speak: {type: string}}, required: [speak]}
"""

MODULE_PY = """\
from typing import Any

from beepboop.module import Module as BaseModule, ToolContext, tool


class Module(BaseModule):
    @tool("dav.read")
    async def read(self, ctx: ToolContext) -> dict[str, Any]:
        r = await ctx.http.request("REPORT", str(ctx.settings["url"]) + "cal/", data="<q/>")
        return {"speak": r.text}

    @tool("dav.poke")
    async def poke(self, ctx: ToolContext) -> dict[str, Any]:
        await ctx.http.request("PROPFIND", str(ctx.settings["url"]), data="<p/>")
        await ctx.http.put(str(ctx.settings["url"]) + "x.ics", data="BEGIN:VCALENDAR")
        return {"speak": "ok"}
"""

EVALS = """\
api: 1
module: dav
fixtures:
  settings: {url: "https://dav.test/"}
  http:
    - match: {method: REPORT, url: "https://dav.test/cal/"}
      response: {status: 207, text: "reported"}
    - match: {method: PROPFIND, url: "https://dav.test/"}
      response: {status: 207, text: "<multistatus/>"}
cases:
  - id: read_sure
    utterance: "read"
    expect: {tool: dav.read, args: {}, speak_contains: ["reported"], effects: []}
  - id: read_unsure_confirms
    utterance: "read"
    voice: {confidence: 0.80, margin: 0.15}
    expect: {tool: dav.read, args: {}, confirm: true}
  - id: poke_records_only_the_put
    utterance: "poke"
    expect:
      tool: dav.poke
      args: {}
      effects: [{kind: http, method: PUT, url: "https://dav.test/x.ics"}]
  - id: poke_unknown_voice_denied
    utterance: "poke"
    voice: {confidence: 0.50, margin: 0.05}
    expect: {tool: dav.poke, args: {}, denied: true}
"""


def _module(tmp_path: Path) -> Path:
    d = tmp_path / "dav"
    d.mkdir()
    (d / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    (d / "module.py").write_text(MODULE_PY, encoding="utf-8")
    (d / "evals.yaml").write_text(EVALS, encoding="utf-8")
    return d


def test_safe_methods() -> None:
    assert frozenset({"GET", "PROPFIND", "REPORT"}) == SAFE_HTTP_METHODS


async def test_evals_serve_webdav_reads_record_writes_and_use_voice(tmp_path: Path) -> None:
    report = await run_evals_async(_module(tmp_path))
    assert report.ok, [(r.case_id, r.messages) for r in report.failed]
    decisions = {r.case_id: r.decision for r in report.results}
    assert decisions == {
        "read_sure": "allow",
        "read_unsure_confirms": "confirm",
        "poke_records_only_the_put": "allow",
        "poke_unknown_voice_denied": "deny",
    }


async def test_fake_resolves_setting_egress_and_refuses_other_hosts() -> None:
    effects: list[dict[str, object]] = []
    fixture = HttpFixture("PROPFIND", "https://dav.test/*", status=207, text="ok")
    http = FakeHttp(
        [fixture], effects, allowed_hosts=["setting:url"], settings={"url": "https://dav.test/x"}
    )
    r = await http.request("PROPFIND", "https://dav.test/dav/")
    assert (r.status_code, r.text) == (207, "ok")
    with pytest.raises(ToolError):
        await http.request("PROPFIND", "https://evil.test/dav/")
    no_settings = FakeHttp([fixture], effects, allowed_hosts=["setting:url"])
    with pytest.raises(ToolError):
        await no_settings.request("PROPFIND", "https://dav.test/dav/")
    assert effects == []


def test_host_allowed_setting_rule() -> None:
    assert host_allowed("https://dav.test/a", ["setting:url"], {"url": "https://DAV.test:8443/"})
    assert not host_allowed("https://dav.test/a", ["setting:url"], {"url": 7})
    assert not host_allowed("https://dav.test/a", ["setting:url"], None)


async def test_httpx_client_sends_str_body_as_content_and_webdav_methods() -> None:
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("content-type", ""), request.content))
        return httpx.Response(207, text="<multistatus/>")

    client = HttpxClient(allowed_hosts=["dav.test"])
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
    try:
        r = await client.request(
            "REPORT", "https://dav.test/cal/", headers={"Depth": "1"}, data="<q/>"
        )
        await client.put(
            "https://dav.test/x.ics",
            headers={"Content-Type": "text/calendar"},
            data="BEGIN:VCALENDAR",
        )
        await client.post("https://dav.test/form", data={"a": "1"})
    finally:
        await client.aclose()
    assert r.status_code == 207
    assert seen[0] == ("REPORT", "", b"<q/>")
    assert seen[1] == ("PUT", "text/calendar", b"BEGIN:VCALENDAR")
    assert seen[2] == ("POST", "application/x-www-form-urlencoded", b"a=1")
