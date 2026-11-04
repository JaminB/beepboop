"""A small in-memory CalDAV server for the caldav module's tests.

``FakeCalDAV.handle`` answers WebDAV/CalDAV requests the way Nextcloud and Radicale do for the
subset the module uses (RFC 4791 discovery, calendar-query REPORT, PUT with If-None-Match).
Two adapters put it on the wire:

* ``FakeCalDAVHttp``: an ``HttpClient`` (MODULES §5.3) calling ``handle`` in-process;
* ``serve``: a real HTTP server on 127.0.0.1, so the core's ``HttpxClient`` (egress check,
  timeouts, raw-body handling) is exercised end to end.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

MULTI_OPEN = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
OK = "<d:status>HTTP/1.1 200 OK</d:status>"


@dataclass
class Calendar:
    name: str
    comps: tuple[str, ...] = ("VEVENT",)
    events: dict[str, str] = field(default_factory=dict[str, str])  # resource name → ICS


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: str


@dataclass
class FakeCalDAV:
    """One user, ``alice``, whose principal and calendar home live under ``/dav/``."""

    username: str = "alice"
    password: str = "app-password"
    calendars: dict[str, Calendar] = field(default_factory=dict[str, Calendar])
    requests: list[Request] = field(default_factory=list[Request])
    fail_next: dict[str, int] = field(default_factory=dict[str, int])  # method → status once

    @property
    def home(self) -> str:
        return f"/dav/calendars/{self.username}/"

    def add_calendar(self, slug: str, cal: Calendar) -> str:
        path = f"{self.home}{slug}/"
        self.calendars[path] = cal
        return path

    # ------------------------------------------------------------------ dispatch --

    def handle(
        self, method: str, url: str, headers: Mapping[str, str], body: str
    ) -> tuple[int, dict[str, str], str]:
        path = urlsplit(url).path or "/"
        hdrs = {k.lower(): v for k, v in headers.items()}
        self.requests.append(Request(method, path, hdrs, body))
        if method in self.fail_next:
            return self.fail_next.pop(method), {}, ""
        if not self._authorised(hdrs.get("authorization", "")):
            return 401, {"WWW-Authenticate": 'Basic realm="fake"'}, ""
        if path == "/.well-known/caldav":
            return 301, {"Location": "/dav/"}, ""
        if path == "/elsewhere/":
            return 301, {"Location": "https://other.example/dav/"}, ""
        if method == "PROPFIND":
            return self._propfind(path, hdrs.get("depth", "0"))
        if method == "REPORT":
            return self._report(path, body)
        if method == "PUT":
            return self._put(path, hdrs, body)
        return 405, {}, ""

    def _authorised(self, header: str) -> bool:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return header == f"Basic {token}"

    def _propfind(self, path: str, depth: str) -> tuple[int, dict[str, str], str]:
        if path == "/dav/":
            principal = f"/dav/principals/{self.username}/"
            props = (
                "<d:resourcetype><d:collection/></d:resourcetype>"
                f"<d:current-user-principal><d:href>{principal}</d:href></d:current-user-principal>"
            )
            return self._multi([(path, props)])
        if path == f"/dav/principals/{self.username}/":
            props = f"<c:calendar-home-set><d:href>{self.home}</d:href></c:calendar-home-set>"
            return self._multi([(path, props)])
        if path == self.home:
            rows = [(path, "<d:resourcetype><d:collection/></d:resourcetype>")]
            if depth == "1":
                rows += [(p, self._cal_props(c)) for p, c in self.calendars.items()]
            return self._multi(rows)
        if path in self.calendars:
            return self._multi([(path, self._cal_props(self.calendars[path]))])
        return 404, {}, ""

    @staticmethod
    def _cal_props(cal: Calendar) -> str:
        comps = "".join(f'<c:comp name="{c}"/>' for c in cal.comps)
        return (
            "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            f"<d:displayname>{_xml(cal.name)}</d:displayname>"
            f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
        )

    def _report(self, path: str, body: str) -> tuple[int, dict[str, str], str]:
        cal = self.calendars.get(path)
        if cal is None:
            return 404, {}, ""
        if not re.search(r'<c:time-range start="\d{8}T\d{6}Z" end="\d{8}T\d{6}Z"/>', body):
            return 400, {}, ""
        rows = [
            (
                f"{path}{name}",
                f"<c:calendar-data>{_xml(ics)}</c:calendar-data>",
            )
            for name, ics in cal.events.items()
        ]
        return self._multi(rows)

    def _put(
        self, path: str, hdrs: Mapping[str, str], body: str
    ) -> tuple[int, dict[str, str], str]:
        parent, _, name = path.rpartition("/")
        cal = self.calendars.get(f"{parent}/")
        if cal is None or not name.endswith(".ics"):
            return 404, {}, ""
        if hdrs.get("if-none-match") == "*" and name in cal.events:
            return 412, {}, ""
        if "BEGIN:VCALENDAR" not in body or "text/calendar" not in hdrs.get("content-type", ""):
            return 415, {}, ""
        cal.events[name] = body
        return 201, {"ETag": json.dumps(name)}, ""

    @staticmethod
    def _multi(rows: list[tuple[str, str]]) -> tuple[int, dict[str, str], str]:
        parts = [
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>{OK}"
            "</d:propstat></d:response>"
            for href, props in rows
        ]
        xml = f'<?xml version="1.0" encoding="utf-8"?>{MULTI_OPEN}{"".join(parts)}</d:multistatus>'
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, xml


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------- in-process adapter --


@dataclass
class FakeResponse:
    status_code: int
    text: str
    headers: Mapping[str, str]

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class FakeCalDAVHttp:
    """``HttpClient`` (MODULES §5.3) that sends every request to a ``FakeCalDAV``."""

    def __init__(self, server: FakeCalDAV, host: str = "cal.test") -> None:
        self.server = server
        self.host = host

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 - the Protocol's signature
    ) -> FakeResponse:
        if urlsplit(url).hostname != self.host:
            msg = f"unexpected host in {url}"
            raise AssertionError(msg)
        status, hdrs, text = self.server.handle(method, url, headers or {}, str(data or ""))
        return FakeResponse(status, text, hdrs)

    async def get(self, url: str, **kw: Any) -> FakeResponse:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> FakeResponse:
        return await self.request("POST", url, **kw)

    async def put(self, url: str, **kw: Any) -> FakeResponse:
        return await self.request("PUT", url, **kw)

    async def patch(self, url: str, **kw: Any) -> FakeResponse:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> FakeResponse:
        return await self.request("DELETE", url, **kw)


# ------------------------------------------------------------------ real HTTP adapter --


@contextmanager
def serve(server: FakeCalDAV) -> Generator[str]:
    """Run ``server`` on an ephemeral 127.0.0.1 port; yields the base URL."""

    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            status, hdrs, text = server.handle(
                self.command, self.path, dict(self.headers.items()), body
            )
            payload = text.encode("utf-8")
            self.send_response(status)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_PROPFIND = do_REPORT = do_PUT = do_GET = _any  # noqa: N815 - http.server naming

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
