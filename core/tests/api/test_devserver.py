"""The fake core behind the Playwright suite behaves like the device (acceptance 5 shape)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from beepboop.api.devserver import SEED_TOTP, build
from beepboop.api.security import totp_code, totp_step
from beepboop.api.testing import PASSWORD

URL = "https://localhost:8787"  # httpx sends Secure cookies over https only
HDR = {"Sec-Fetch-Site": "same-origin", "Origin": URL}


async def _login(http: AsyncClient, user: str) -> None:
    from datetime import UTC, datetime

    code = totp_code(SEED_TOTP[user], totp_step(datetime.now(UTC)))
    r = await http.post(
        "/api/auth/login", headers=HDR, json={"username": user, "password": PASSWORD, "totp": code}
    )
    assert r.status_code == 200, r.text


async def test_fake_device_speaks_outcomes(tmp_path: Path) -> None:
    app = await build(tmp_path, None, URL, seeded=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=URL) as http:
        await _login(http, "alex")
        r = await http.post("/api/_dev/voice", headers=HDR, json={"profile": "Alex"})
        assert r.json()["outcome"] == "confirm"
        approval_id = r.json()["approval_id"]
        # The nonce is only on the bus (the SSE stream); read it the way the stream does.
        deps = app.state.deps
        nonce = deps.events.nonce_for(approval_id)
        r = await http.post(
            f"/api/approvals/{approval_id}/approve", headers=HDR, json={"nonce": nonce}
        )
        assert r.status_code == 200
        await asyncio.sleep(0.1)
        said = (await http.get("/api/_dev/speech")).json()
        assert said[-1] == "Unlocked."
        # Expiry: the device says so.
        r = await http.post("/api/_dev/voice", headers=HDR, json={"profile": "Alex"})
        second = r.json()["approval_id"]
        second_nonce = deps.events.nonce_for(second)
        await http.post("/api/_dev/clock", headers=HDR, json={"advance_s": 61})
        said = (await http.get("/api/_dev/speech")).json()
        assert "timed out" in said[-1]
        r = await http.post(
            f"/api/approvals/{second}/approve",
            headers=HDR,
            json={"nonce": second_nonce},
        )
        assert r.status_code == 409
        assert r.json()["code"] == "expired"
    await deps.db.close()


async def test_unseeded_fake_core_offers_a_claim_code(tmp_path: Path) -> None:
    app = await build(tmp_path, None, URL, seeded=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=URL) as http:
        code = (await http.get("/api/_dev/claim")).json()["code"]
        assert code is not None
        r = await http.post("/api/onboarding/claim", headers=HDR, json={"code": code})
        assert r.status_code == 200
    await app.state.deps.db.close()
