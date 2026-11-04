"""M0 item V5: the passphrase-wrapped KEK works on a Pi; the OTP key is optional.

ROADMAP V5 (after owner decision D3, 2026-11-03): the argon2id passphrase-wrapped
KEK, the v0.1 default, works on a Pi board without an OTP key. The OTP part
(the key is readable from the core container via ``/dev/vcio`` and produces a
stable KEK across reboots) is an optional later upgrade: it is still recorded
when run, but it does not decide V5's status.

Subcommands (all append one observation to
``tools/bench/results/<date>-<host>-v5.json`` and recompute the verdict):

``read``      read the OTP key through ``/dev/vcio`` with the production code
              (:func:`beepboop.secrets.read_otp_key`), derive the KEK and record
              its *fingerprint*, the kernel ``boot_id`` and whether this ran in
              a container. ``--cross-check`` (host only) also reads the key with
              ``vcmailbox`` and records whether the two agree.
``fallback``  exercise the passphrase-wrapped KEK in a scratch directory: create,
              reload, reject a wrong passphrase, and round-trip a secret through
              :class:`beepboop.secrets.SecretStore`.
``report``    recompute and print the verdict from the recorded observations.

The OTP key and the KEK are never printed or written; only a one-way
fingerprint is. Nothing here writes OTP (that is irreversible; the installer
does not do it either, D3). Off a Pi every record is labelled
"synthetic / x86 / not a gate result".
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from beepboop.secrets import (
    Argon2Params,
    DecryptionError,
    Kek,
    OtpUnavailableError,
    SecretRef,
    SecretStore,
    create_passphrase_kek,
    derive_otp_kek,
    load_device_salt,
    load_passphrase_kek,
    read_otp_key,
)
from beepboop.secrets.otp import VCIO_PATH, parse_response

SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_DIR = REPO_ROOT / "tools" / "bench" / "results"
SYNTHETIC_LABEL = "synthetic / x86 / not a gate result"
VCMAILBOX_ARGS = ["0x00030081", "40", "40", "0", "8", "0", "0", "0", "0", "0", "0", "0", "0"]

Status = Literal["PASS", "FAIL", "INCOMPLETE"]
type JsonObject = dict[str, object]
OtpReader = Callable[[], bytes | None]


# ------------------------------------------------------------ environment --


def board_model() -> str | None:
    """``/proc/device-tree/model``, e.g. "Raspberry Pi 4 Model B Rev 1.5"."""
    try:
        return Path("/proc/device-tree/model").read_bytes().rstrip(b"\x00").decode()
    except OSError:
        return None


def boot_id() -> str | None:
    """The kernel's per-boot UUID (shared with containers, so it tracks reboots)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None


def in_container() -> bool:
    """Best-effort Docker/containerd detection."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def short_hash(data: bytes) -> str:
    """Identify the device salt between runs without revealing it."""
    return hashlib.sha256(b"beepboop/v5-salt-id\x00" + data).hexdigest()[:16]


# ---------------------------------------------------------- observations --


def observe_read(
    otp_reader: OtpReader, salt: bytes, *, cross_check: OtpReader | None
) -> JsonObject:
    """Read the OTP key once and describe the result without revealing it."""
    obs: JsonObject = {
        "kind": "otp-read",
        "boot_id": boot_id(),
        "in_container": in_container(),
        "vcio_present": VCIO_PATH.exists(),
        "salt_id": short_hash(salt),
        "otp_programmed": None,
        "kek_fingerprint": None,
        "cross_check": None,
        "error": None,
    }
    try:
        key = otp_reader()
    except OtpUnavailableError as exc:
        obs["error"] = str(exc)
        return obs
    obs["otp_programmed"] = key is not None
    if key is not None:
        obs["kek_fingerprint"] = derive_otp_kek(key, salt).fingerprint()
    if cross_check is not None:
        obs["cross_check"] = _cross_check(key, salt, cross_check)
    return obs


def _cross_check(key: bytes | None, salt: bytes, reader: OtpReader) -> JsonObject:
    try:
        other = reader()
    except (OSError, ValueError, OtpUnavailableError, subprocess.SubprocessError) as exc:
        return {"tool": "vcmailbox", "match": None, "error": str(exc)}
    same = (key is None and other is None) or (
        key is not None
        and other is not None
        and derive_otp_kek(key, salt).fingerprint() == derive_otp_kek(other, salt).fingerprint()
    )
    return {"tool": "vcmailbox", "match": same, "error": None}


def read_with_vcmailbox() -> bytes | None:
    """Read the key with Raspberry Pi's ``vcmailbox`` tool (host only), for --cross-check."""
    tool = shutil.which("vcmailbox")
    if tool is None:
        msg = "vcmailbox not installed (Pi OS: raspi-utils)"
        raise OtpUnavailableError(msg)
    out = subprocess.run(  # noqa: S603 - fixed argv, resolved tool path, no shell
        [tool, *VCMAILBOX_ARGS], capture_output=True, text=True, check=True, timeout=10
    ).stdout
    words = [int(token, 16) for token in out.split()]
    key = parse_response(words)
    return None if key == bytes(len(key)) else key


def observe_fallback(otp_reader: OtpReader, params: Argon2Params | None = None) -> JsonObject:
    """Exercise the passphrase-wrapped KEK end to end in a scratch directory."""
    try:
        programmed = otp_reader() is not None
    except OtpUnavailableError:
        programmed = False
    obs: JsonObject = {
        "kind": "passphrase-fallback",
        "on_board_without_otp": not programmed,
        "ok": False,
        "detail": "",
    }
    with tempfile.TemporaryDirectory(prefix="bb-v5-") as scratch:
        root = Path(scratch)
        salt = load_device_salt(root / "keys", create=True)
        passphrase = secrets.token_urlsafe(16)
        path = root / "keys" / "kek.passphrase.wrapped"
        created = create_passphrase_kek(path, passphrase, salt, params=params)
        reloaded = load_passphrase_kek(path, passphrase, salt)
        if reloaded != created:
            obs["detail"] = "reloaded KEK differs from the created one"
            return obs
        try:
            load_passphrase_kek(path, passphrase + "x", salt)
        except DecryptionError:
            pass
        else:
            obs["detail"] = "a wrong passphrase was accepted"
            return obs
        value = secrets.token_hex(16)
        stored = asyncio.run(_store_round_trip(root / "secrets.db", reloaded, value))
        obs["ok"] = stored == value
        obs["detail"] = "create, reload, wrong-passphrase rejection, store round trip"
    return obs


async def _store_round_trip(db: Path, kek: Kek, value: str) -> str:
    ref = SecretRef("v5_probe")
    async with await SecretStore.open(db, kek) as store:
        await store.put(ref, value)
    async with await SecretStore.open(db, kek) as store:
        return (await store.get(ref)).get_secret_value()


# --------------------------------------------------------------- verdict --


def otp_verdict(observations: list[JsonObject]) -> tuple[Status, str]:
    """PASS: >= 2 boots, same fingerprint, read at least once inside a container."""
    reads = [o for o in observations if o["kind"] == "otp-read" and o["otp_programmed"]]
    fingerprints = {o["kek_fingerprint"] for o in reads}
    salts = {o["salt_id"] for o in reads}
    if any(_cross_check_failed(o) for o in reads):
        return "FAIL", "ioctl read and vcmailbox disagree"
    if len(salts) > 1:
        return "INCOMPLETE", "device salt changed between runs; keep --data-dir fixed and redo"
    if len(fingerprints) > 1:
        return "FAIL", "KEK fingerprint changed between reads"
    boots = {o["boot_id"] for o in reads if o["boot_id"]}
    containers = [o for o in reads if o["in_container"]]
    if len(boots) >= 2 and containers:
        return "PASS", f"same KEK on {len(boots)} boots, read inside a container"
    return "INCOMPLETE", (
        f"{len(boots)} boot(s) with a programmed key, {len(containers)} read(s) in a container;"
        " need >= 2 boots and >= 1 container read"
    )


def _cross_check_failed(observation: JsonObject) -> bool:
    cross = observation.get("cross_check")
    return isinstance(cross, dict) and cast("JsonObject", cross).get("match") is False


def fallback_verdict(observations: list[JsonObject]) -> tuple[Status, str]:
    runs = [o for o in observations if o["kind"] == "passphrase-fallback"]
    if not runs:
        return "INCOMPLETE", "no fallback run recorded"
    latest = runs[-1]
    if not latest["ok"]:
        return "FAIL", str(latest["detail"])
    if not any(o["ok"] and o["on_board_without_otp"] for o in runs):
        return "INCOMPLETE", "fallback works, but not yet run on a board without an OTP key"
    return "PASS", str(latest["detail"])


def overall(fallback: Status) -> Status:
    """V5's status is the passphrase path's (D3); the OTP verdict is informational."""
    return fallback


# ---------------------------------------------------------------- record --


def results_path(results_dir: Path, host: str, today: datetime) -> Path:
    """Reuse this host's existing V5 file (observations span reboots and days)."""
    existing = sorted(results_dir.glob(f"*-{host}-v5.json"))
    return existing[0] if existing else results_dir / f"{today:%Y-%m-%d}-{host}-v5.json"


def update_record(
    path: Path, observation: JsonObject | None, *, host: str, model: str | None, now: datetime
) -> JsonObject:
    """Append ``observation`` (if any), recompute verdicts and write ``path``."""
    record: JsonObject = (
        cast("JsonObject", json.loads(path.read_text(encoding="utf-8")))
        if path.exists()
        else {
            "schema": SCHEMA,
            "probe": "V5",
            "item": "passphrase-wrapped KEK (v0.1 default, D3); optional OTP key -> stable KEK",
            "host": host,
            "observations": [],
        }
    )
    observations = cast("list[JsonObject]", record["observations"])
    if observation is not None:
        observations.append({"recorded_at": now.isoformat(), "board_model": model, **observation})
    otp_status, otp_note = otp_verdict(observations)
    fb_status, fb_note = fallback_verdict(observations)
    record["otp"] = {"status": otp_status, "note": otp_note, "optional": True}
    record["fallback"] = {"status": fb_status, "note": fb_note}
    record["status"] = overall(fb_status)
    models = {o.get("board_model") for o in observations}
    if any(not (isinstance(m, str) and m.startswith("Raspberry Pi")) for m in models):
        record["label"] = SYNTHETIC_LABEL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


# ------------------------------------------------------------------ main --


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M0 V5: OTP KEK probe")
    parser.add_argument("command", choices=["read", "fallback", "report"])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("v5-data"),
        help="holds keys/device_salt; keep it across reboots (default: ./v5-data)",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--host", default=platform.node() or "unknown")
    parser.add_argument(
        "--cross-check", action="store_true", help="read: also read the key with vcmailbox"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, otp_reader: OtpReader = read_otp_key) -> int:
    args = parse_args(argv)
    now = datetime.now(UTC)
    observation: JsonObject | None = None
    if args.command == "read":
        salt = load_device_salt(args.data_dir / "keys", create=True)
        cross = read_with_vcmailbox if args.cross_check else None
        observation = observe_read(otp_reader, salt, cross_check=cross)
    elif args.command == "fallback":
        observation = observe_fallback(otp_reader)
    path = results_path(args.results_dir, args.host, now)
    record = update_record(path, observation, host=args.host, model=board_model(), now=now)
    otp, fallback = cast("JsonObject", record["otp"]), cast("JsonObject", record["fallback"])
    sys.stdout.write(
        f"V5 {record['status']}  otp={otp['status']} ({otp['note']})"
        f"  fallback={fallback['status']} ({fallback['note']})\n  -> {path}\n"
    )
    return 1 if record["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
