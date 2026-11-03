"""M0 item V2: record headless HA onboarding against the real, pinned HA container.

ROADMAP V2: "HA headless onboarding (``/api/onboarding/*``) and
``auth/long_lived_access_token`` over WebSocket work from core using the
onboarding session; record the exact sequence."

This runs the production code path (:class:`beepboop.ha.onboarding.HAOnboarding`)
against a **fresh** HA volume, then checks what it produced:

1. ``onboarding``: the run returns ``onboarded`` and ``GET /api/onboarding``
   reports every step done.
2. ``llat_auth``: the stored long-lived token (decrypted from the secrets
   envelope) authenticates a new WebSocket (``auth_ok``).
3. ``llat_lifespan``: the token's JWT ``exp - iat`` is the requested 3650 days
   (the payload is decoded locally; the signature is not needed for that).
4. ``llat_registered``: ``auth/refresh_tokens`` lists exactly one
   ``long_lived_access_token`` named ``beepboop-core``.
5. ``llat_on_llat`` (informational, HOME_ASSISTANT §3's open note): can a
   connection authenticated *with* an LLAT mint another one? The probe token is
   deleted again with ``auth/delete_refresh_token``.

The observed request sequence (method, path, status, timing, response *key
names*) and the checks go to ``tools/bench/results/<date>-<host>-v2.json``.
No token, code or password is printed or written. Run it with ``uv run``
from the repo root on the Pi; ``verify_v2.md`` is the owner procedure.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import aiohttp
from pydantic import SecretStr

from beepboop.ha.onboarding import (
    HA_STEPS,
    HA_TOKEN_SECRET,
    HAOnboarding,
    OnboardingConfig,
    OnboardingError,
    OnboardingResult,
)
from beepboop.ha.ws import HAWebSocket, HAWebSocketError
from beepboop.log import configure_logging
from beepboop.secrets import (
    SecretStore,
    load_device_salt,
    read_otp_key,
    read_passphrase_file,
    resolve_kek,
)

SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_DIR = REPO_ROOT / "tools" / "bench" / "results"
SYNTHETIC_LABEL = "synthetic / x86 / not a gate result"
PROBE_CLIENT_NAME = "beepboop-v2-probe"

Status = Literal["PASS", "FAIL", "INCOMPLETE"]
type JsonObject = dict[str, object]


# ----------------------------------------------------------------- helpers --


def board_model() -> str | None:
    """``/proc/device-tree/model`` (e.g. "Raspberry Pi 4 Model B Rev 1.5"), if any."""
    try:
        return Path("/proc/device-tree/model").read_bytes().rstrip(b"\x00").decode()
    except OSError:
        return None


def is_raspberry_pi(model: str | None) -> bool:
    return model is not None and model.startswith("Raspberry Pi")


def jwt_lifespan_days(token: SecretStr) -> float | None:
    """``(exp - iat) / 86400`` from an HA access token's JWT payload; ``None`` if absent."""
    parts = token.get_secret_value().split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload: object = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    claims = cast("JsonObject", payload)
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float) or not isinstance(iat, int | float):
        return None
    return (exp - iat) / 86400


def check(name: str, passed: bool | None, detail: str, *, gating: bool = True) -> JsonObject:
    """One check row. ``passed=None`` means not observed (never guessed)."""
    return {"name": name, "passed": passed, "detail": detail, "gating": gating}


def verdict(checks: list[JsonObject]) -> Status:
    """FAIL if any gating check failed, INCOMPLETE if any is unobserved, else PASS."""
    gating = [c["passed"] for c in checks if c["gating"]]
    if any(p is False for p in gating):
        return "FAIL"
    if not gating or any(p is None for p in gating):
        return "INCOMPLETE"
    return "PASS"


# ------------------------------------------------------------------ checks --


async def verify_token(
    session: aiohttp.ClientSession, config: OnboardingConfig, token: SecretStr, *, mint_probe: bool
) -> list[JsonObject]:
    """Checks 2-5 on a fresh WebSocket authenticated with the stored LLAT."""
    lifespan = jwt_lifespan_days(token)
    lifespan_ok = None if lifespan is None else abs(lifespan - config.llat_lifespan_days) < 1
    checks = [
        check("llat_lifespan", lifespan_ok, f"jwt exp-iat = {lifespan} days"),
    ]
    try:
        async with HAWebSocket(session, config.websocket_url, token) as ws:
            checks.append(check("llat_auth", True, f"auth_ok, ha_version={ws.ha_version}"))
            tokens = await ws.call("auth/refresh_tokens")
            llats = [
                t
                for t in cast("list[JsonObject]", tokens if isinstance(tokens, list) else [])
                if t.get("type") == "long_lived_access_token"
                and t.get("client_name") == config.llat_client_name
            ]
            checks.append(
                check(
                    "llat_registered",
                    len(llats) == 1,
                    f"{len(llats)} long-lived token(s) named {config.llat_client_name!r}",
                )
            )
            if mint_probe:
                checks.append(await _llat_on_llat(ws))
    except HAWebSocketError as exc:
        checks.append(check("llat_auth", False, f"{type(exc).__name__}: {exc}"))
    return checks


async def _llat_on_llat(ws: HAWebSocket) -> JsonObject:
    """Informational: mint a throwaway LLAT on an LLAT session, then delete it."""
    try:
        await ws.call("auth/long_lived_access_token", client_name=PROBE_CLIENT_NAME, lifespan=1)
    except HAWebSocketError as exc:
        return check("llat_on_llat", False, f"refused: {exc}", gating=False)
    tokens = await ws.call("auth/refresh_tokens")
    for entry in cast("list[JsonObject]", tokens if isinstance(tokens, list) else []):
        if entry.get("client_name") == PROBE_CLIENT_NAME:
            await ws.call("auth/delete_refresh_token", refresh_token_id=str(entry["id"]))
    return check("llat_on_llat", True, "allowed; probe token deleted again", gating=False)


# -------------------------------------------------------------------- main --


@dataclass
class ProbeOutcome:
    """What one run observed: the onboarding result (if it finished), checks and the trace."""

    result: OnboardingResult | None
    checks: list[JsonObject]
    sequence: list[JsonObject] = field(default_factory=list[JsonObject])


async def run_probe(
    config: OnboardingConfig, store: SecretStore, *, mint_probe: bool
) -> ProbeOutcome:
    """Onboard, then verify. A failed step is recorded with the partial sequence."""
    async with aiohttp.ClientSession() as session:
        onboarding = HAOnboarding(session, store, config)
        try:
            result = await onboarding.run()
        except (OnboardingError, HAWebSocketError) as exc:
            failed = check("onboarding", False, f"{type(exc).__name__}: {exc}")
            return ProbeOutcome(None, [failed], _sequence(onboarding))
        if result.status != "onboarded":
            detail = f"status={result.status}: V2 needs a FRESH HA volume (see verify_v2.md)"
            return ProbeOutcome(result, [check("onboarding", None, detail)], _sequence(onboarding))
        done = all(result.steps_after.get(step, False) for step in HA_STEPS)
        checks = [check("onboarding", done, f"steps_after={result.steps_after}")]
        token = await store.get(HA_TOKEN_SECRET)
        checks += await verify_token(session, config, token, mint_probe=mint_probe)
        return ProbeOutcome(result, checks, _sequence(onboarding))


def _sequence(onboarding: HAOnboarding) -> list[JsonObject]:
    return [step.model_dump(mode="json") for step in onboarding.trace.steps]


def build_record(
    outcome: ProbeOutcome,
    *,
    config: OnboardingConfig,
    kek_kind: str,
    now: datetime,
    host: str,
    model: str | None,
) -> JsonObject:
    """The results-file JSON. Shapes and verdicts only; no secret values."""
    result = outcome.result
    record: JsonObject = {
        "schema": SCHEMA,
        "probe": "V2",
        "item": "HA headless onboarding + auth/long_lived_access_token over WebSocket",
        "recorded_at": now.isoformat(),
        "host": host,
        "board_model": model,
        "ha_url": config.ha_url,
        "ha_version": result.ha_version if result else None,
        "kek_kind": kek_kind,
        "onboarding_status": result.status if result else "error",
        "sequence": outcome.sequence,
        "checks": outcome.checks,
        "status": verdict(outcome.checks),
    }
    if not is_raspberry_pi(model):
        record["label"] = SYNTHETIC_LABEL
    return record


def write_record(record: JsonObject, results_dir: Path, now: datetime, host: str) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{now:%Y-%m-%d}-{host}-v2.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--ha-url", default="http://127.0.0.1:8123")
    parser.add_argument("--client-id", default="https://beepboop.local/")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("v2-data"),
        help="where keys/ and the secrets database go (default: ./v2-data)",
    )
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        default=os.environ.get("BB_KEK_PASSPHRASE_FILE"),
        help="KEK passphrase for boards without a programmed OTP key",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--no-llat-on-llat", action="store_true", help="skip the informational check 5"
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    configure_logging()
    keys_dir = args.data_dir / "keys"
    load_device_salt(keys_dir, create=True)
    passphrase = read_passphrase_file(args.passphrase_file) if args.passphrase_file else None
    kek = resolve_kek(
        keys_dir, otp_reader=read_otp_key, passphrase=passphrase, create_passphrase_file=True
    )
    config = OnboardingConfig(ha_url=args.ha_url, client_id=args.client_id)
    async with await SecretStore.open(args.data_dir / "secrets.db", kek) as store:
        outcome = await run_probe(config, store, mint_probe=not args.no_llat_on_llat)
    now = datetime.now(UTC)
    host = platform.node() or "unknown"
    record = build_record(
        outcome, config=config, kek_kind=kek.kind, now=now, host=host, model=board_model()
    )
    path = write_record(record, args.results_dir, now, host)
    sys.stdout.write(f"V2 {record['status']}: wrote {path}\n")
    return 1 if record["status"] == "FAIL" else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
