"""Regressions from the v0.1 security review: session-bound turn ids, admin exports,
re-authentication for TOTP replacement, the reserved ``core`` module id, strict CSRF and
no Swagger UI outside the dev server."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from beepboop.api.security import new_totp_secret
from beepboop.api.testing import PASSWORD, Api, register_frontdoor, voice_principal
from beepboop.db import repo
from beepboop.db.repo.turns import TurnRow
from beepboop.policy import NeedsConfirmation
from beepboop.registry.manifest import ManifestError, parse_manifest

from .test_onboarding_auth import _claim_and_create_admin  # pyright: ignore[reportPrivateUsage]


async def _voice_turn(api: Api, profile_id: str, text: str) -> str:
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
    )
    await api.db.write(lambda c: repo.turns.insert_turn(c, row))
    return tid


async def test_turn_id_header_is_ignored_and_not_me_is_profile_bound(api: Api) -> None:
    """A member cannot flag another adult's turn through a practice approval + not-me."""
    alex, _tok = await api.person("Alex", "admin")
    _sam, sam_tok = await api.person("Sam", "member")
    alex_turn = await _voice_turn(api, alex, "unlock the front door")
    api.use(sam_tok)
    from beepboop.api.testing import SAME_ORIGIN

    r = await api.http.post(
        "/api/approvals/practice", headers={**SAME_ORIGIN, "x-turn-id": alex_turn}
    )
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["turn_id"] != alex_turn
    assert view["turn_id"].startswith("web-")
    assert view["via"] is None  # a practice approval reveals no other turn's details
    nonce = api.deps.events.nonce_for(view["id"])
    assert nonce is not None
    r = await api.post(f"/api/approvals/{view['id']}/not-me", json={"nonce": nonce})
    assert r.status_code == 200
    turn = await api.db.read(lambda c: repo.turns.get_turn(c, alex_turn))
    assert turn is not None
    assert turn.flagged is False
    assert turn.corrected_profile_id is None


async def test_not_me_flags_only_the_profiles_own_voice_turn(api: Api) -> None:
    register_frontdoor(api.deps.registry)
    alex, tok = await api.person("Alex", "admin")
    tid = await _voice_turn(api, alex, "unlock the front door")
    with pytest.raises(NeedsConfirmation) as exc:
        await api.deps.registry.dispatch(voice_principal(alex, turn_id=tid), "frontdoor.unlock", {})
    api.use(tok)
    r = await api.post(
        f"/api/approvals/{exc.value.approval.id}/not-me", json={"nonce": exc.value.nonce}
    )
    assert r.status_code == 200
    turn = await api.db.read(lambda c: repo.turns.get_turn(c, tid))
    assert turn is not None
    assert turn.flagged is True


async def test_admin_export_of_another_adult_has_no_transcripts(api: Api) -> None:
    """WEB_UI §3 and DATA_AND_PRIVACY §9.2: admins may export anyone, but another adult's
    words stay out; a child's export and one's own export keep them."""
    _alex, admin = await api.person("Alex", "admin")
    sam, member = await api.person("Sam", "member")
    robin, _ = await api.person("Robin", "child", login=False)
    await _voice_turn(api, sam, "what's on my calendar")
    await _voice_turn(api, robin, "turn on the lights")

    async def export(pid: str) -> list[dict[str, object]]:
        r = await api.post(f"/api/privacy/{pid}/export")
        assert r.status_code == 200, r.text
        r = await api.http.get(f"/api/privacy/{pid}/export/{r.json()['export_id']}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        lines = z.read("turns.jsonl").decode().splitlines()
        return [json.loads(ln) for ln in lines if "approval" not in ln]

    api.use(admin)
    sam_turns = await export(sam)
    assert sam_turns
    assert all(t["transcript"] is None and t["reply"] is None for t in sam_turns)
    robin_turns = await export(robin)
    assert robin_turns[0]["transcript"] == "turn on the lights"
    api.use(member)
    own = await export(sam)
    assert own[0]["transcript"] == "what's on my calendar"


async def test_totp_replace_needs_password_and_current_factor(api: Api) -> None:
    secret, codes = await _claim_and_create_admin(api)
    fresh = new_totp_secret()
    api.clock.advance(60)
    body = {"secret": fresh, "code": api.totp_now(fresh)}
    r = await api.post("/api/auth/totp", json=body)
    assert r.status_code == 422  # current_password is required
    r = await api.post("/api/auth/totp", json={**body, "current_password": "wrong one!"})
    assert r.json()["code"] == "bad_credentials"
    r = await api.post("/api/auth/totp", json={**body, "current_password": PASSWORD})
    assert r.json()["code"] == "totp_required"
    r = await api.post(
        "/api/auth/totp",
        json={**body, "current_password": PASSWORD, "current_totp": "000000"},
    )
    assert r.json()["code"] == "bad_totp"
    r = await api.post(
        "/api/auth/totp",
        json={**body, "current_password": PASSWORD, "current_totp": api.totp_now(secret)},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["recovery_codes"]) == 10
    # The old recovery codes are gone with the old factor; a new one plus the password works.
    api.clock.advance(60)
    newer = new_totp_secret()
    r = await api.post(
        "/api/auth/totp",
        json={
            "secret": newer,
            "code": api.totp_now(newer),
            "current_password": PASSWORD,
            "recovery_code": codes[0],
        },
    )
    assert r.json()["code"] == "bad_recovery_code"


async def test_password_change_signs_out_other_sessions(api: Api) -> None:
    sam, tok = await api.person("Sam")
    other = "tok-other"
    await api.db.write(
        lambda c: repo.sessions.create(c, repo.sessions.hash_token(other), sam), durable=True
    )
    api.use(tok)
    r = await api.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "a much better passphrase"},
    )
    assert r.status_code == 204
    assert (await api.http.get("/api/me")).status_code == 200
    api.use(other)
    assert (await api.http.get("/api/me")).status_code == 401


async def test_missing_origin_is_rejected(api: Api) -> None:
    r = await api.http.post("/api/auth/logout", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    assert r.json()["code"] == "bad_origin"


async def test_swagger_ui_is_off_outside_the_devserver(api: Api) -> None:
    assert (await api.http.get("/api/docs")).status_code == 404
    assert (await api.http.get("/api/openapi.json")).status_code == 200


def test_core_module_id_is_reserved() -> None:
    text = """
id: core
api: 1
version: 0.1.0
summary: "Pretends to be the registry."
scope: household
category: system
tools: []
"""
    with pytest.raises(ManifestError, match="reserved"):
        parse_manifest(text)
