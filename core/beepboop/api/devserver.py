"""A fake core for UI development and the Playwright suite (``ui/tests/e2e``).

``python -m beepboop.api.devserver --port 8787 --spa ui/build [--seed]`` serves the real
``/api`` (real routers, policy, approvals, SQLite in a temp dir, envelope secrets under a
throwaway KEK) plus the built SPA, and adds a dev-only ``/api/_dev`` router that stands in
for the device:

* ``POST /api/_dev/voice`` - "someone said it": dispatch a tool as a confident voice
  principal. A ``critical`` tool raises the approval exactly like the voice path does; the
  fake executor then waits for the decision and "speaks" the outcome.
* ``GET /api/_dev/speech`` - what the device said.
* ``POST /api/_dev/clock`` - move the clock (expire an approval without waiting 60 s).
* ``GET /api/_dev/claim`` - the claim code the device would speak.

The ``_dev`` router is never part of ``create_app``/OpenAPI; nothing in production
imports this module. Nothing here is a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from beepboop.agent.phrases import PHRASES
from beepboop.api.deps import ApiDeps
from beepboop.api.problem import ApiError
from beepboop.api.routers.approvals import expire_due
from beepboop.api.routers.onboarding import open_first_claim
from beepboop.api.security import hash_recovery_code, new_recovery_codes
from beepboop.api.testing import CHEAP, PASSWORD, build_api, register_frontdoor, voice_principal
from beepboop.db import repo
from beepboop.policy.errors import NeedsConfirmation, PolicyDenied

#: Fixed TOTP secrets for the seeded people, so a test can compute codes.
SEED_TOTP = {"alex": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", "sam": "KRSXG5CTMVRXEZLUKRSXG5CTMVRXEZLU"}


@dataclass
class FakeDevice:
    deps: ApiDeps
    said: list[str] = field(default_factory=list[str])
    claim_code: str | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    async def execute_when_decided(self, approval_id: str, profile_id: str, tool: str) -> None:
        """The executor half of IDENTITY_AND_TRUST §8.2, as the agent loop does it."""
        record = await self.deps.events.wait_decision(approval_id, 130)
        if record is None or record.decision == "expired":
            self.said.append(PHRASES["approval.expired"])
            return
        if record.decision == "denied":
            self.said.append(PHRASES["approval.denied"])
            return
        try:
            out = await self.deps.registry.dispatch(
                voice_principal(profile_id), tool, {}, approval=record
            )
        except PolicyDenied:
            self.said.append(PHRASES["approval.expired"])
            return
        self.said.append(str(out.result.structured.get("speak") or "Done."))


class VoiceBody(BaseModel):
    profile: str
    tool: str = "frontdoor.unlock"
    confidence: float = 0.95


class ClockBody(BaseModel):
    advance_s: float


def dev_router(device: FakeDevice, advance: Any) -> APIRouter:
    router = APIRouter(prefix="/api/_dev", tags=["_dev"], include_in_schema=False)
    deps = device.deps

    @router.post("/voice")
    async def voice(body: VoiceBody) -> dict[str, Any]:
        profile = await deps.db.read(lambda c: repo.profiles.by_name(c, body.profile))
        if profile is None:
            raise ApiError(404, "no_such_profile")
        principal = voice_principal(profile.id, confidence=body.confidence)
        try:
            out = await deps.registry.dispatch(principal, body.tool, {})
        except NeedsConfirmation as exc:
            device_line = PHRASES["approval.prompt"].format(summary=exc.approval.summary)
            device.said.append(device_line)
            task = asyncio.create_task(
                device.execute_when_decided(exc.approval.id, profile.id, body.tool)
            )
            device.tasks.add(task)
            task.add_done_callback(device.tasks.discard)
            return {"outcome": "confirm", "approval_id": exc.approval.id}
        except PolicyDenied as exc:
            return {"outcome": "deny", "reason": exc.reason}
        device.said.append(str(out.result.structured.get("speak") or "Done."))
        return {"outcome": "allow"}

    @router.get("/speech")
    async def speech() -> list[str]:
        return device.said

    @router.post("/clock")
    async def clock(body: ClockBody) -> dict[str, str]:
        advance(body.advance_s)
        await expire_due(deps)
        await asyncio.sleep(0.05)  # let a woken executor speak
        return {"now": deps.clock().isoformat()}

    @router.get("/claim")
    async def claim() -> dict[str, str | None]:
        return {"code": device.claim_code}

    return router


async def seed(deps: ApiDeps) -> None:
    """Alex (admin) and Sam (member), each with password + fixed TOTP; Robin (child)."""
    from beepboop.api.people import create_profile
    from beepboop.api.routers.auth import seal_totp

    for name, preset in (("Alex", "admin"), ("Sam", "member"), ("Robin", "child")):
        if await deps.db.read(lambda c, n=name: repo.profiles.by_name(c, n)) is not None:
            continue
        row = await deps.db.write(
            lambda c, n=name, p=preset: create_profile(c, n, p, created_by=None),  # type: ignore[arg-type]
            durable=True,
        )
        username = name.lower()
        if username not in SEED_TOTP:
            continue
        await deps.db.write(
            lambda c, r=row, u=username: repo.logins.insert(c, r.id, u, CHEAP.hash(PASSWORD)),
            durable=True,
        )
        sealed = await seal_totp(deps, row.id, SEED_TOTP[username])
        codes = new_recovery_codes()
        login = await deps.db.read(lambda c, r=row: repo.logins.get(c, r.id))
        assert login is not None  # noqa: S101
        final = replace(
            login,
            totp_secret_enc=sealed,
            recovery_hashes_json=json.dumps([hash_recovery_code(x) for x in codes]),
        )
        await deps.db.write(lambda c, f=final: repo.logins.update(c, f), durable=True)
    await deps.db.write(lambda c: repo.household.set_(c, "onboarding_done", True), durable=True)
    await deps.refresh_policy()


async def build(data_dir: Path, spa: Path | None, public_url: str, *, seeded: bool) -> FastAPI:
    deps, _db, clock, app = await build_api(
        data_dir, public_url=public_url, spa_dir=None, frozen=False, docs=True
    )
    register_frontdoor(deps.registry)
    device = FakeDevice(deps)

    def on_code(code: str, _mode: str) -> None:
        device.claim_code = code

    deps.claim.on_code = on_code
    if seeded:
        await seed(deps)
    else:
        await open_first_claim(deps)
    app.include_router(dev_router(device, clock.advance))
    if spa is not None:  # checked to be a directory by main()
        from beepboop.api.app import SpaFiles

        app.mount("/", SpaFiles(directory=str(spa), html=True), name="spa")
    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--spa", type=Path, default=None)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--public-url", default=None)
    args = ap.parse_args(argv)
    public_url = args.public_url or f"http://localhost:{args.port}"
    data_dir = Path(tempfile.mkdtemp(prefix="bb-fake-core-"))
    spa: Path | None = args.spa if args.spa is not None and args.spa.is_dir() else None

    async def run() -> None:
        app = await build(data_dir, spa, public_url, seeded=args.seed)
        server = uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        )
        await server.serve()

    asyncio.run(run())


if __name__ == "__main__":
    main()
