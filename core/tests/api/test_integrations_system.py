"""Integrations forms, System tabs, enrollment, Home, and the static SPA (WEB_UI §2, §5)."""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from beepboop.api.events import Viewer
from beepboop.api.ports import RingLog
from beepboop.api.testing import PUBLIC, Api, build_api, register_frontdoor
from beepboop_protocol import EnrollClipData, EnrollQuality


async def test_integration_cards_forms_and_write_only_secrets(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    _alex, admin = await api.person("Alex", "admin")
    _sam, member = await api.person("Sam", "member")
    api.use(member)
    cards = (await api.http.get("/api/integrations")).json()
    assert [c["module_id"] for c in cards] == ["frontdoor"]
    r = await api.put("/api/integrations/frontdoor/settings", json={"values": {"label": "x"}})
    assert r.status_code == 403  # household settings are admin-only
    api.use(admin)
    detail = (await api.http.get("/api/integrations/frontdoor")).json()
    assert detail["settings_schema"]["properties"]["label"]["title"] == "Door name"
    assert detail["household"]["secrets"] == [
        {"name": "api_key", "title": "API key", "description": None, "required": True, "set": False}
    ]
    r = await api.put(
        "/api/integrations/frontdoor/settings", json={"values": {"auto_lock_minutes": 99}}
    )
    assert r.status_code == 422
    assert set(r.json()["fields"]) == {"label", "auto_lock_minutes"}
    r = await api.put(
        "/api/integrations/frontdoor/settings",
        json={"values": {"label": "Back door"}, "secrets": {"api_key": "s3cr3t-value"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["secrets"][0]["set"] is True
    assert "s3cr3t-value" not in r.text
    r = await api.delete("/api/integrations/frontdoor/secrets/api_key")
    assert r.status_code == 204


async def test_system_tabs(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    _sam, member = await api.person("Sam", "member")
    api.use(member)
    r = await api.http.get("/api/system/health")
    assert r.status_code == 200
    assert r.json()["host"] is None  # members read Health, not host.json
    assert (await api.http.get("/api/system/updates")).status_code == 403
    api.use(admin)
    (api.data_dir / "host.json").write_text('{"temp_c": 61}', encoding="utf-8")
    h = (await api.http.get("/api/system/health")).json()
    assert h["host"] == {"temp_c": 61}
    assert h["audio"] == "offline"
    u = (await api.http.get("/api/system/updates")).json()
    assert u == {
        "current": "test",
        "available": None,
        "changelog_url": None,
        "command": "beepboop update",
        "notify_only": True,
    }
    llm = (await api.http.get("/api/system/llm")).json()
    assert llm["values"] == {}
    r = await api.put(
        "/api/system/llm",
        json={
            "values": {"region": "eu-central-1", "model_id": "m"},
            "secrets": {"aws_access_key_id": "AKIA"},
        },
    )
    assert r.status_code == 200
    assert {s["name"]: s["set"] for s in r.json()["secrets"]} == {
        "aws_access_key_id": True,
        "aws_secret_access_key": False,
    }
    assert (await api.http.get("/api/system/https")).json()["fingerprint"] is None
    assert (await api.http.get("/api/system/logs")).json() == []
    assert (await api.http.get("/api/system/backups")).json()["only_local"] is True
    ring = RingLog(size=2)
    for i in range(3):
        ring(None, "info", {"event": f"e{i}", "turn_id": "t1" if i else None})
    api.deps.logs = ring
    assert [x["event"] for x in (await api.http.get("/api/system/logs")).json()] == ["e1", "e2"]


class _FakeAudio:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def start(self, session_id: str, profile_id: str, prompts: list[str]) -> None:
        self.started.append((session_id, profile_id, len(prompts)))

    async def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)


async def test_enrollment_flow_with_fake_audio(api: Api) -> None:
    sam, member = await api.person("Sam", "member")
    robin, _ = await api.person("Robin", "child", login=False)
    api.use(member)
    r = await api.post("/api/enrollments", json={"profile_id": sam, "consent": True})
    assert r.status_code == 503
    assert r.json()["code"] == "audio_offline"
    audio = _FakeAudio()
    api.deps.enrollment = audio
    assert (
        await api.post("/api/enrollments", json={"profile_id": robin, "consent": True})
    ).status_code == 404
    r = await api.post("/api/enrollments", json={"profile_id": sam, "consent": True})
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["total"] == 15
    assert audio.started == [(view["session_id"], sam, 15)]
    hub = api.deps.enroll_hub
    assert hub is not None
    q = api.deps.events.subscribe(Viewer(sam, "member"))[1]
    quality = EnrollQuality(snr_db=22.0, clipping=0.0, speech_ratio=0.8, duration_ms=1200)
    hub.clip(
        EnrollClipData(session_id=view["session_id"], index=0, accepted=False, quality=quality)
    )
    for i in range(15):
        hub.clip(
            EnrollClipData(
                session_id=view["session_id"],
                index=i + 1,
                accepted=True,
                quality=quality,
                embedding=[0.1] * 512,
            )
        )
    first = q.get_nowait()
    assert first.event == "enroll"
    assert first.data["accepted"] is False
    assert "embedding" not in first.data
    r = await api.http.get(f"/api/enrollments/{view['session_id']}")
    assert r.json()["done"] is True
    r = await api.post(f"/api/enrollments/{view['session_id']}/finish")
    assert r.status_code == 503
    assert r.json()["code"] == "voiceprint_builder_unavailable"


async def test_home_and_household(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    api.use(admin)
    home = (await api.http.get("/api/home")).json()
    assert home["name"] == "Alex"
    assert {a["kind"] for a in home["attention"]} >= {"voice"}
    r = await api.patch("/api/household", json={"name": "Maple St", "timezone": "Mars/Base"})
    assert r.status_code == 422
    r = await api.patch(
        "/api/household",
        json={
            "transcript_retention_days": 0,
            "guest_turn_retention_days": 0,
            "audit_retention_days": 90,
        },
    )
    assert r.status_code == 200, r.text  # DATA_AND_PRIVACY §7: 0 = timing and decisions only
    assert r.json()["transcript_retention_days"] == 0
    assert r.json()["guest_turn_retention_days"] == 0
    assert r.json()["audit_retention_days"] == 90
    r = await api.patch("/api/household", json={"audit_retention_days": 30})
    assert r.status_code == 422
    r = await api.patch("/api/household", json={"name": "Maple St", "timezone": "Europe/Berlin"})
    assert r.json()["name"] == "Maple St"
    assert (await api.http.get("/api/ca")).json()["fingerprint"] is None


async def test_spa_fallback_cache_and_security_headers(tmp_path: Path) -> None:
    spa = tmp_path / "spa"
    (spa / "_app" / "immutable").mkdir(parents=True)
    (spa / "index.html").write_text("<!doctype html><title>bb</title>", encoding="utf-8")
    (spa / "_app" / "immutable" / "app.abc.js").write_text("1", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    _deps, db, _clock, app = await build_api(data, spa_dir=spa)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC) as http:
        r = await http.get("/people/01ABC/voice")
        assert r.status_code == 200
        assert "<title>bb</title>" in r.text
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["permissions-policy"] == "microphone=()"
        r = await http.get("/_app/immutable/app.abc.js")
        assert "immutable" in r.headers["cache-control"]
        r = await http.get("/api/nope")
        assert r.status_code == 404
        assert "<title>" not in r.text
    await db.close()
