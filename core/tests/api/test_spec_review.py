"""Spec regressions from the v0.1 review: System > Voice, Integrations extras (title,
SETUP.md, x-entity picker, per-profile overrides, health failure wording), guest kv
retention and the spoken-name / via rules for denials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from beepboop.agent.phrases import deny_phrase, deny_speech
from beepboop.api.testing import Api
from beepboop.db import repo, retention, to_ms
from beepboop.db.repo.ha_entities import EntityRow
from beepboop.module import Health, Module, ToolContext, tool
from beepboop.policy import Grant
from beepboop.registry.manifest import Manifest

WEATHERISH: dict[str, Any] = {
    "id": "wx",
    "api": 1,
    "version": "0.1.0",
    "title": "Weather",
    "spoken_name": "weather",
    "summary": "Pretend weather for tests.",
    "scope": "household",
    "category": "information",
    "settings": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "title": "Source", "x-entity": {"domains": ["weather"]}},
            "units": {
                "type": "string",
                "enum": ["metric", "imperial"],
                "default": "metric",
                "x-per-profile": True,
            },
        },
    },
    "tools": [
        {
            "name": "wx.now",
            "description": "Now.",
            "risk": "read",
            "untrusted_output": False,
            "args": {"type": "object", "properties": {}, "additionalProperties": False},
            "output": {"type": "object", "properties": {"speak": {"type": "string"}}},
        }
    ],
}


class Wx(Module):
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash

    @tool("wx.now")
    async def now(self, ctx: ToolContext) -> dict[str, Any]:
        return {"speak": "Sunny."}

    async def health(self, ctx: ToolContext) -> Health:
        if self.crash:
            raise KeyError("entity")
        return Health("ok", "fine")


def _register(api: Api, *, crash: bool = False) -> None:
    api.deps.registry.register_module(
        Manifest.model_validate(WEATHERISH),
        Wx(crash=crash),
        "# wx\n",
        "# Setup\n\n1. Pick a source.\n",
    )


async def test_voice_settings_round_trip_and_policy_reload(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    api.use(admin)
    r = await api.http.get("/api/system/voice")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["thresholds"] == {
        "tau_act": 0.7, "margin_act": 0.1, "tau_personal": 0.9, "margin_personal": 0.2
    }  # fmt: skip
    assert view["ask_when_unsure"] is True
    assert view["false_wakes_7d"] == 0
    assert view["wake_sensitivity"] is None  # set in /config/beepboop.yaml, not here
    r = await api.patch(
        "/api/system/voice",
        json={
            "thresholds": {
                "tau_act": 0.75, "margin_act": 0.1, "tau_personal": 0.92, "margin_personal": 0.25
            },
            "ask_when_unsure": False,
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["thresholds"]["tau_personal"] == 0.92
    state = api.deps.policy_state()
    assert state.ask_when_unsure is False
    assert state.profiles[_alex].calibration.tau_personal == 0.92  # the household default
    r = await api.patch(
        "/api/system/voice",
        json={
            "thresholds": {
                "tau_act": 0.9, "margin_act": 0.1, "tau_personal": 0.8, "margin_personal": 0.2
            }
        },
    )  # fmt: skip
    assert r.status_code == 422
    audit = await api.db.read(lambda c: repo.audit.list_(c))
    assert any(a.action == "voice.updated" for a in audit)
    _sam, member = await api.person("Sam", "member")
    api.use(member)
    assert (await api.http.get("/api/system/voice")).status_code == 403


async def test_integration_title_setup_md_and_entity_picker(api: Api) -> None:
    _register(api)
    alex, admin = await api.person("Alex", "admin")
    await api.db.write(
        lambda c: (
            repo.ha_entities.upsert(
                c, EntityRow("u-wx", "weather.home", "weather", "active", friendly_name="Home")
            ),
            repo.ha_entities.upsert(
                c, EntityRow("u-new", "weather.new", "weather", "new", friendly_name="New")
            ),
            repo.ha_entities.upsert(
                c, EntityRow("u-lamp", "light.lamp", "light", "active", friendly_name="Lamp")
            ),
            repo.grants.insert(c, Grant("g-wx", "profile", alex, "ha_entity", "u-wx", "read")),
            repo.grants.insert(c, Grant("g-lamp", "profile", alex, "ha_entity", "u-lamp", "act")),
        ),
        durable=True,
    )
    await api.deps.refresh_policy()
    api.use(admin)
    cards = (await api.http.get("/api/integrations")).json()
    card = next(c for c in cards if c["module_id"] == "wx")
    assert card["title"] == "Weather"
    detail = (await api.http.get("/api/integrations/wx")).json()
    assert detail["title"] == "Weather"
    assert detail["setup_md"].startswith("# Setup")
    assert detail["per_profile_keys"] == ["units"]
    assert detail["mine"] is not None  # "Your settings" exists on a household module
    r = await api.http.get("/api/integrations/wx/entities", params={"domains": "weather"})
    assert r.status_code == 200, r.text
    assert [e["uuid"] for e in r.json()] == ["u-wx"]  # granted and active; not the inbox one
    r = await api.http.get("/api/integrations/wx/entities")
    assert {e["uuid"] for e in r.json()} == {"u-wx", "u-lamp"}


async def test_per_profile_override_on_a_household_module(api: Api) -> None:
    _register(api)
    _alex, admin = await api.person("Alex", "admin")
    sam, member = await api.person("Sam", "member")
    api.use(admin)
    r = await api.put("/api/integrations/wx/settings", json={"values": {"units": "metric"}})
    assert r.status_code == 200, r.text
    api.use(member)
    r = await api.put(
        "/api/integrations/wx/settings", json={"values": {"units": "imperial"}, "profile_id": sam}
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile_id"] == sam
    assert r.json()["settings"] == {"units": "imperial"}
    r = await api.put(
        "/api/integrations/wx/settings", json={"values": {"entity": "u-wx"}, "profile_id": sam}
    )
    assert r.status_code == 422  # not an x-per-profile key
    assert r.json()["fields"] == {"entity": "not_per_profile"}
    r = await api.put("/api/integrations/wx/settings", json={"values": {"units": "imperial"}})
    assert r.status_code == 403  # the household row is admin-only
    detail = (await api.http.get("/api/integrations/wx")).json()
    assert detail["mine"]["settings"] == {"units": "imperial"}


async def test_health_failure_is_logged_and_explained(api: Api) -> None:
    from beepboop.api.routers.integrations import run_health

    _register(api, crash=True)
    manifest = api.deps.registry.manifest("wx")
    assert manifest is not None
    status, detail = await run_health(api.deps, manifest, None)
    assert status == "ok"  # the test registry has no hook-context factory: no check ran
    assert detail == ""


def test_health_failure_message_names_the_module_and_a_trace() -> None:
    import asyncio
    import tempfile
    from pathlib import Path

    from beepboop.api.routers.integrations import run_health
    from beepboop.api.testing import build_api

    class Contexts:
        def for_hook(self, module_id: str, profile_id: str | None = None) -> ToolContext:
            from beepboop.testing import fake_context

            return fake_context(Path(__file__).resolve().parents[3] / "docs" / "module-template")

    async def run() -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as d:
            deps, db, _clock, _app = await build_api(Path(d), context_factory=Contexts())
            try:
                deps.registry.register_module(
                    Manifest.model_validate(WEATHERISH), Wx(crash=True), "# wx\n"
                )
                manifest = deps.registry.manifest("wx")
                assert manifest is not None
                return await run_health(deps, manifest, None)
            finally:
                await db.close()

    status, detail = asyncio.run(run())
    assert status == "error"
    assert detail.startswith("The Weather check failed unexpectedly.")
    assert "System > Logs (trace " in detail
    assert "KeyError" not in detail


async def test_guest_kv_is_swept_with_guest_retention(api: Api) -> None:
    now = datetime.now(UTC)
    old = to_ms(now - timedelta(days=8))

    def _seed(c: Any) -> None:
        repo.kv.set_(c, "timers", "guest", "t1", {"label": "old"})
        c.execute("UPDATE kv SET updated_at = ? WHERE key = 't1'", (old,))
        repo.kv.set_(c, "timers", "guest", "t2", {"label": "fresh"})

    await api.db.write(_seed, durable=True)
    counts = await api.db.write(lambda c: retention.nightly_purge(c, now), durable=True)
    assert counts.guest_kv == 1
    keys = await api.db.read(lambda c: repo.kv.keys(c, "timers", "guest"))
    assert keys == ["t2"]


def test_denial_phrase_depends_on_via_and_spoken_name() -> None:
    assert deny_phrase("no_grant", "guest", "voice") == "unknown_speaker"
    assert deny_phrase("no_grant", "guest", "web") == "not_granted"
    typed = deny_speech("no_grant", "guest", tool="wx.now", name="Guest", admin="Alex", via="web")
    assert typed.text is not None
    assert "recognise your voice" not in typed.text
    assert "wx" in typed.text  # the raw namespace without a spoken name
    named = deny_speech(
        "no_grant", "member", tool="wx.now", name="Sam", admin="Alex", spoken_name="forecast"
    )
    assert (
        named.text == "You don't have access to the forecast. An adult can change that in the app."
    )
