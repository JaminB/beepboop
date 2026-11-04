"""People, Access, guest access, inbox, Activity visibility, privacy (WEB_UI §5.3-§5.10)."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from beepboop.agent import ListSpeaker, MemoryConversation, TurnLoop
from beepboop.agent.context import PromptInputs
from beepboop.agent.types import Utterance
from beepboop.api.routers.chat import DryRunRegistry
from beepboop.api.testing import Api, register_frontdoor, voice_principal
from beepboop.db import repo
from beepboop.db.repo.ha_entities import EntityRow
from beepboop.db.repo.turns import ToolCallRow, TurnRow
from beepboop.policy.types import GUEST_PROFILE_ID


async def test_people_visibility_and_admin_only_fields(api: Api) -> None:
    alex, admin = await api.person("Alex", "admin")
    sam, member = await api.person("Sam", "member")
    robin, _ = await api.person("Robin", "child", login=False)
    api.use(member)
    r = await api.http.get("/api/profiles")
    assert [p["name"] for p in r.json()] == ["Sam"]
    assert (await api.http.get(f"/api/profiles/{alex}")).status_code == 404
    r = await api.patch(f"/api/profiles/{sam}", json={"verbosity": "brief", "speech_rate": 1.2})
    assert r.status_code == 200
    assert r.json()["verbosity"] == "brief"
    r = await api.patch(f"/api/profiles/{sam}", json={"risk_ceiling": "critical"})
    assert r.status_code == 403
    assert r.json()["code"] == "admin_only"
    api.use(admin)
    r = await api.http.get("/api/profiles")
    assert {p["name"] for p in r.json()} == {"Guest", "Alex", "Sam", "Robin"}
    r = await api.patch(f"/api/profiles/{robin}", json={"risk_ceiling": "read"})
    assert r.json()["risk_ceiling"] == "read"


async def test_presets_and_access_view(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    _alex, admin = await api.person("Alex", "admin")
    api.use(admin)
    r = await api.post(
        "/api/profiles", json={"name": "Robin", "preset": "child", "quiet_hours": True}
    )
    assert r.status_code == 201
    child = r.json()["profile"]
    assert child["risk_ceiling"] == "act"
    assert child["groups"] == ["children", "household"]
    assert r.json()["invite"] is None
    r = await api.http.get(f"/api/profiles/{child['id']}/access")
    body = r.json()
    media = next(g for g in body["grants"] if g["resource_id"] == "media.*")
    assert media["schedule"] is not None
    tools = {t["name"]: t["allowed"] for m in body["modules"] for t in m["tools"]}
    assert tools == {"frontdoor.unlock": False, "frontdoor.status": False}
    r = await api.http.get(f"/api/grants/export/{child['id']}")
    assert "risk_ceiling: act" in r.text


async def test_delete_me_refuses_the_last_admin_and_needs_the_name(api: Api) -> None:
    alex, admin = await api.person("Alex", "admin")
    sam, _member = await api.person("Sam", "member")
    api.use(admin)
    r = await api.delete(f"/api/profiles/{alex}", params={"confirm": "Alex"})
    assert r.status_code == 409
    assert r.json()["code"] == "last_admin"
    r = await api.delete(f"/api/profiles/{sam}", params={"confirm": "sam"})
    assert r.status_code == 422
    r = await api.delete(f"/api/profiles/{sam}", params={"confirm": "Sam"})
    assert r.status_code == 204
    assert await api.db.read(lambda c: repo.profiles.get(c, sam)) is None


async def test_pin_is_sealed_and_never_returned(api: Api) -> None:
    sam, member = await api.person("Sam", "member")
    api.use(member)
    r = await api.put(f"/api/profiles/{sam}/pin", json={"pin": "12ab"})
    assert r.status_code == 422
    r = await api.put(f"/api/profiles/{sam}/pin", json={"pin": "4821"})
    assert r.status_code == 204
    row = await api.db.read(lambda c: repo.profiles.get(c, sam))
    assert row is not None
    assert row.pin_hash_enc is not None
    assert b"argon2" not in row.pin_hash_enc  # sealed, not the bare hash
    r = await api.http.get(f"/api/profiles/{sam}")
    assert r.json()["pin_set"] is True
    assert "4821" not in r.text
    r = await api.delete(f"/api/profiles/{sam}/pin")
    assert r.status_code == 204


async def test_guest_access_is_scheduled_grants(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    api.use(admin)
    r = await api.put("/api/guest-access", json={"area_ids": ["living_room"]})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    assert r.json()["area_ids"] == ["living_room"]
    rows = await api.db.read(lambda c: repo.grants.list_for_subject(c, "profile", GUEST_PROFILE_ID))
    ours = [g for g in rows if g.source == "guest_access"]
    assert {g.resource_id for g in ours} == {
        "home.*",
        "media.*",
        "timer.*",
        "list.read",
        "living_room",
    }
    assert all(g.schedule is not None and g.schedule.expires_at is not None for g in ours)
    r = await api.delete("/api/guest-access")
    assert r.status_code == 204
    assert (await api.http.get("/api/guest-access")).json()["active"] is False


async def _entity(api: Api, uuid: str, domain: str, device_class: str | None = None) -> None:
    row = EntityRow(
        uuid=uuid,
        entity_id=f"{domain}.{uuid}",
        domain=domain,
        device_class=device_class,
        area_id="hall",
        friendly_name=uuid.title(),
    )
    await api.db.write(lambda c: repo.ha_entities.upsert(c, row), durable=True)


async def test_inbox_defaults_apply_and_undo(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    _sam, member = await api.person("Sam", "member")
    await _entity(api, "garage", "cover", "garage")
    await _entity(api, "plug", "switch")
    await _entity(api, "movie", "scene")
    api.use(member)
    assert (await api.http.get("/api/integrations/ha/inbox")).status_code == 403
    api.use(admin)
    items = {i["uuid"]: i for i in (await api.http.get("/api/integrations/ha/inbox")).json()}
    assert items["garage"]["risk"] == "critical"
    assert items["garage"]["default_choice"] == "adults"
    assert items["plug"]["default_choice"] == "everyone"
    assert items["movie"]["lowerable"] is True
    # Lowering a direct critical service is refused; lowering an indirect one is allowed.
    r = await api.post(
        "/api/integrations/ha/inbox",
        json={"decisions": [{"uuid": "garage", "choice": "everyone", "risk": "act"}]},
    )
    assert r.status_code == 422
    r = await api.post(
        "/api/integrations/ha/inbox",
        json={
            "decisions": [
                {"uuid": "garage", "choice": "adults"},
                {"uuid": "plug", "choice": "everyone"},
                {"uuid": "movie", "choice": "everyone", "risk": "act"},
            ]
        },
    )
    assert r.json() == {"grants_written": 3, "remaining": 0}
    movie = await api.db.read(lambda c: repo.ha_entities.get(c, "movie"))
    assert movie is not None
    assert movie.declared_risk == "act"
    r = await api.post("/api/integrations/ha/inbox/undo", json={"uuids": ["plug"]})
    assert r.json()["remaining"] == 1


async def _turn(api: Api, profile_id: str, text: str, **kw: Any) -> str:
    from beepboop_protocol import new_ulid

    tid = new_ulid()
    row = TurnRow(
        id=tid,
        profile_id=profile_id,
        via="voice",
        locale="en-US",
        path="fast",
        stt_provider="s",
        tts_provider="t",
        transcript=text,
        reply="Done.",
        confidence=0.88,
        stages_ms={"endpoint": 250, "stt_tail": 100},
        **kw,
    )
    await api.db.write(lambda c: repo.turns.insert_turn(c, row))
    call = ToolCallRow(
        turn_id=tid,
        seq=1,
        tool="home.turn_off",
        module_id="ha",
        args_redacted={"area": "garden"},
        effective_risk="act",
        decision="allow",
        result_status="ok",
        profile_id=profile_id,
    )
    await api.db.write(lambda c: repo.turns.insert_tool_call(c, call))
    return tid


async def test_activity_visibility_rules(api: Api) -> None:
    alex, admin = await api.person("Alex", "admin")
    sam, member = await api.person("Sam", "member")
    robin, _ = await api.person("Robin", "child", login=False)
    t_sam = await _turn(api, sam, "read my calendar")
    t_robin = await _turn(api, robin, "turn off the garden lights")
    t_alex = await _turn(api, alex, "set a timer")
    api.use(admin)
    page = (await api.http.get("/api/turns", params={"who": "everyone"})).json()
    by_id = {t["id"]: t for t in page["items"]}
    assert by_id[t_robin]["transcript"] == "turn off the garden lights"  # child: in full
    assert by_id[t_sam]["transcript"] is None  # other adult: calls only
    assert by_id[t_sam]["transcript_hidden"] is True
    assert by_id[t_alex]["transcript"] == "set a timer"
    detail = (await api.http.get(f"/api/turns/{t_sam}")).json()
    assert detail["tool_calls"][0]["tool"] == "home.turn_off"
    assert detail["transcript"] is None
    api.use(member)
    page = (await api.http.get("/api/turns", params={"who": "everyone"})).json()
    assert [t["id"] for t in page["items"]] == [t_sam]  # members: only their own
    assert (await api.http.get(f"/api/turns/{t_robin}")).status_code == 404
    r = await api.post(f"/api/turns/{t_sam}/not-me", json={})
    assert r.json()["did"] == ["Turn off garden"]
    turn = await api.db.read(lambda c: repo.turns.get_turn(c, t_sam))
    assert turn is not None
    assert turn.flagged
    r = await api.post(f"/api/turns/{t_sam}/false-wake")
    assert r.status_code == 204
    r = await api.delete(f"/api/turns/{t_sam}")
    assert r.status_code == 204


async def test_privacy_ledger_and_export(api: Api) -> None:
    sam, member = await api.person("Sam", "member")
    await _turn(api, sam, "what's the weather")
    api.use(member)
    ledger = (await api.http.get(f"/api/privacy/{sam}")).json()
    kept = {k["key"]: k for k in ledger["kept"]}
    assert kept["transcripts"]["count"] == 1
    assert kept["audio"]["detail"] == "none"
    assert ledger["audio_kept"] is False
    r = await api.post(f"/api/privacy/{sam}/export")
    assert r.status_code == 200, r.text
    export_id = r.json()["export_id"]
    r = await api.http.get(f"/api/privacy/{sam}/export/{export_id}")
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {
        "profile.json",
        "grants.json",
        "turns.jsonl",
        "modules.json",
        "integrations.json",
        "voiceprint.json",
        "audit.jsonl",
        "README.txt",
    }
    profile = json.loads(z.read("profile.json"))
    assert "pin_hash_enc" not in profile
    assert "pw_hash" not in json.dumps(profile)
    assert profile["login"] == {"username": "sam", "totp": False}
    # Downloadable once.
    assert (await api.http.get(f"/api/privacy/{sam}/export/{export_id}")).status_code == 404
    r = await api.delete(f"/api/privacy/{sam}/transcripts")
    assert r.json() == 1


def _loop(api: Api) -> TurnLoop:
    def inputs(u: Utterance) -> PromptInputs:
        return PromptInputs(
            voice_contract="",
            persona_block="",
            module_cards="",
            core_tools=[],
            profile_id=u.principal.profile_id,
            local_time="12:00",
        )

    return TurnLoop(api.deps.registry, ListSpeaker(), MemoryConversation(), inputs)


async def test_chat_rules(api: Api) -> None:
    _alex, admin = await api.person("Alex", "admin")
    _sam, member = await api.person("Sam", "member")
    api.use(admin)
    r = await api.post("/api/chat", json={"text": "hello"})
    assert r.status_code == 503
    api.deps.loop = _loop(api)
    r = await api.post("/api/chat", json={"text": "what's the weather"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "offline"
    assert r.json()["reply"]
    r = await api.post("/api/chat", json={"text": "hi", "as_profile_id": GUEST_PROFILE_ID})
    assert r.json()["as_profile_id"] == GUEST_PROFILE_ID
    api.use(member)
    r = await api.post("/api/chat", json={"text": "hi", "as_profile_id": GUEST_PROFILE_ID})
    assert r.status_code == 403
    assert r.json()["code"] == "test_as_admin_only"


async def test_dry_run_registry_sets_ctx_dry_run(api: Api) -> None:
    door = register_frontdoor(api.deps.registry)
    alex, _ = await api.person("Alex", "admin")
    await api.deps.registry.dispatch(voice_principal(alex), "frontdoor.status", {})
    wrapped = DryRunRegistry.wrap(api.deps.registry, lambda _t: True)
    out = await wrapped.dispatch(voice_principal(alex), "frontdoor.status", {})
    assert door.dry_runs == [False, True]
    assert out.result.structured["speak"] == "Locked."
