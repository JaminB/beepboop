"""Claim code → first admin → login with TOTP, invites, sessions (WEB_UI §5.1, §5.1b, §6)."""

from __future__ import annotations

from beepboop.api.deps import CLAIM_COOKIE, SESSION_COOKIE
from beepboop.api.routers.onboarding import open_first_claim
from beepboop.api.security import new_totp_secret
from beepboop.api.testing import PASSWORD, Api
from beepboop.db import repo


async def _claim_and_create_admin(api: Api) -> tuple[str, list[str]]:
    code = await open_first_claim(api.deps)
    assert code is not None
    r = await api.post("/api/onboarding/claim", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json() == {"mode": "first_admin"}
    assert CLAIM_COOKIE in r.cookies
    secret = new_totp_secret()
    r = await api.post(
        "/api/onboarding/admin",
        json={
            "name": "Alex",
            "username": "alex",
            "password": PASSWORD,
            "totp_secret": secret,
            "totp_code": api.totp_now(secret),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["me"]["kind"] == "admin"
    assert len(body["recovery_codes"]) == 10
    assert SESSION_COOKIE in r.cookies
    return secret, body["recovery_codes"]


async def test_first_boot_claim_creates_the_admin(api: Api) -> None:
    r = await api.http.get("/api/onboarding/state")
    assert r.json()["claimed"] is False
    secret, _codes = await _claim_and_create_admin(api)
    # The session cookie set by onboarding works.
    r = await api.http.get("/api/me")
    assert r.status_code == 200
    assert r.json()["scopes"] == ["admin", "self"]
    # TOTP is stored sealed, never in plaintext.
    login = await api.db.read(lambda c: repo.logins.by_username(c, "alex"))
    assert login is not None
    assert login.totp_secret_enc is not None
    assert secret.encode() not in login.totp_secret_enc
    assert login.pw_hash.startswith("$argon2id$")
    # Claimed: no new first-admin claim code is issued.
    assert await open_first_claim(api.deps) is None
    r = await api.http.get("/api/onboarding/state")
    assert r.json()["claimed"] is True


async def test_claim_rejects_wrong_codes_and_a_second_admin(api: Api) -> None:
    code = await open_first_claim(api.deps)
    assert code is not None
    wrong = "000000" if code != "000000" else "111111"
    r = await api.post("/api/onboarding/claim", json={"code": wrong})
    assert r.status_code == 401
    assert r.json()["code"] == "claim_wrong"
    # Without a claim session, /admin is refused.
    r = await api.post(
        "/api/onboarding/admin",
        json={
            "name": "X",
            "username": "x1",
            "password": PASSWORD,
            "totp_secret": "A" * 32,
            "totp_code": "000000",
        },
    )
    assert r.status_code == 401
    await _claim_and_create_admin(api)
    # A reopened claim (recovery mode) cannot create another admin.
    code = api.deps.claim.issue(api.clock(), "recovery")
    api.http.cookies.clear()
    r = await api.post("/api/onboarding/claim", json={"code": code})
    assert r.json() == {"mode": "recovery"}
    secret = new_totp_secret()
    r = await api.post(
        "/api/onboarding/admin",
        json={
            "name": "Mallory",
            "username": "mallory",
            "password": PASSWORD,
            "totp_secret": secret,
            "totp_code": api.totp_now(secret),
        },
    )
    assert r.status_code == 403
    assert r.json()["code"] == "claim_recovery_only"


async def test_claim_session_cannot_call_other_apis(api: Api) -> None:
    code = await open_first_claim(api.deps)
    assert code is not None
    await api.post("/api/onboarding/claim", json={"code": code})
    assert (await api.http.get("/api/me")).status_code == 401
    assert (await api.http.get("/api/profiles")).status_code == 401


async def test_recovery_code_signs_the_admin_in_after_onboarding(api: Api) -> None:
    _secret, codes = await _claim_and_create_admin(api)
    api.http.cookies.clear()
    code = api.deps.claim.issue(api.clock(), "recovery")
    await api.post("/api/onboarding/claim", json={"code": code})
    r = await api.post(
        "/api/onboarding/recover", json={"username": "alex", "recovery_code": "nope-nope"}
    )
    assert r.status_code == 401
    r = await api.post(
        "/api/onboarding/recover", json={"username": "alex", "recovery_code": codes[0]}
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Alex"


async def test_login_password_then_totp_and_replay(api: Api) -> None:
    secret, codes = await _claim_and_create_admin(api)
    api.http.cookies.clear()
    r = await api.post("/api/auth/login", json={"username": "alex", "password": "wrong password!"})
    assert r.status_code == 401
    assert r.json()["code"] == "bad_credentials"
    r = await api.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD})
    assert r.json()["code"] == "bad_credentials"  # no user enumeration
    r = await api.post("/api/auth/login", json={"username": "alex", "password": PASSWORD})
    assert r.status_code == 401
    assert r.json()["code"] == "totp_required"
    api.clock.advance(60)
    totp = api.totp_now(secret)
    r = await api.post(
        "/api/auth/login", json={"username": "Alex", "password": PASSWORD, "totp": totp}
    )
    assert r.status_code == 200, r.text
    assert SESSION_COOKIE in r.cookies
    # The same code again is a replay.
    api.http.cookies.clear()
    r = await api.post(
        "/api/auth/login", json={"username": "alex", "password": PASSWORD, "totp": totp}
    )
    assert r.json()["code"] == "bad_totp"
    # A recovery code works once.
    r = await api.post(
        "/api/auth/login",
        json={"username": "alex", "password": PASSWORD, "recovery_code": codes[3]},
    )
    assert r.status_code == 200
    api.http.cookies.clear()
    r = await api.post(
        "/api/auth/login",
        json={"username": "alex", "password": PASSWORD, "recovery_code": codes[3]},
    )
    assert r.json()["code"] == "bad_recovery_code"


async def test_login_lockout_returns_429_with_retry_after(api: Api) -> None:
    await _claim_and_create_admin(api)
    api.http.cookies.clear()
    for _ in range(5):
        await api.post("/api/auth/login", json={"username": "alex", "password": "nope nope nope"})
    r = await api.post("/api/auth/login", json={"username": "alex", "password": PASSWORD})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    audit = await api.db.read(lambda c: repo.audit.list_(c))
    assert any(a.action == "login.locked" for a in audit)


async def test_logout_sessions_and_sign_out_everywhere(api: Api) -> None:
    pid, token = await api.person("Sam")
    api.use(token)
    r = await api.http.get("/api/auth/sessions")
    assert len(r.json()) == 1
    assert r.json()[0]["current"] is True
    r = await api.post(f"/api/profiles/{pid}/sign-out-everywhere")
    assert r.status_code == 204
    assert (await api.http.get("/api/me")).status_code == 401


async def test_invite_flow_creates_a_member_login(api: Api) -> None:
    _admin_id, admin = await api.person("Alex", "admin")
    api.use(admin)
    r = await api.post("/api/profiles", json={"name": "Jo", "preset": "member"})
    assert r.status_code == 201, r.text
    invite = r.json()["invite"]
    assert invite["url"].startswith("https://beepboop.local/invite/")
    r = await api.http.get("/api/invites")
    assert [i["name"] for i in r.json()] == ["Jo"]
    token = invite["token"]
    api.use(None)
    r = await api.http.get(f"/api/invites/{token}")
    assert r.json() == {"name": "Jo", "kind": "member", "totp_required": True}
    # A member must set up TOTP.
    r = await api.post(
        f"/api/invites/{token}/accept", json={"username": "jo", "password": PASSWORD}
    )
    assert r.status_code == 422
    secret = new_totp_secret()
    r = await api.post(
        f"/api/invites/{token}/accept",
        json={
            "username": "jo",
            "password": PASSWORD,
            "totp_secret": secret,
            "totp_code": api.totp_now(secret),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["me"]["kind"] == "member"
    # Single use.
    api.use(None)
    r = await api.http.get(f"/api/invites/{token}")
    assert r.status_code == 404


async def test_password_change_enforces_policy(api: Api) -> None:
    _pid, token = await api.person("Sam")
    api.use(token)
    r = await api.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
    )
    assert r.status_code == 422
    assert r.json()["code"] == "password_too_short"
    r = await api.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "a much better passphrase"},
    )
    assert r.status_code == 204


async def test_csrf_and_problem_json(api: Api) -> None:
    r = await api.http.post(
        "/api/auth/login",
        json={"username": "a", "password": "b"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "cross_site_request"
    r = await api.post("/api/auth/login", json={"username": ""})
    assert r.status_code == 422
    assert "password" in r.json()["fields"]
