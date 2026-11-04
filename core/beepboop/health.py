"""The health report behind ``GET /api/health`` details (docs/OPERATIONS.md §11 owns the fields).

The api builder mounts :func:`collect_health`; everything it reads is a seam so the
report is honest on x86 with no hardware (``None`` = not known yet, never a guess):

* the database: schema version, the last backup (household ``backup.last``), the
  last 24 h of ``turns.stages_ms_json`` for p50/p95 per stage;
* ``/hostrun/host.json`` from ``beepboop-host.timer`` (per-service memory/CPU and
  container health, throttle bits, temperature, disk, reboot pending, chrony offset);
* the Tier-0 root certificate mounted at ``/caddy/root.crt`` for cert expiry;
* :class:`HealthProbes` for live state other builders own (HA connection, audio
  device, Bedrock reachability and last TTFT, pending update).

``beepboop health --quiet`` (the compose healthcheck) only asks ``GET /api/health``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from beepboop.db import repo
from beepboop.db.database import Database
from beepboop.db.ids import to_ms

#: host.json older than this is reported stale (the timer writes it every 10 s).
HOST_JSON_STALE = timedelta(seconds=60)
#: Throttle bits from ``vcgencmd get_throttled`` that mean "now" (OPERATIONS §8).
THROTTLE_NOW = {0x1: "under-voltage", 0x2: "arm frequency capped", 0x4: "throttled",
                0x8: "soft temperature limit"}  # fmt: skip
#: Bits 16-19: the same, "has happened since boot".
THROTTLE_SINCE_BOOT_SHIFT = 16

ServiceState = Literal["ok", "starting", "unhealthy", "down", "unknown"]


class ServiceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    state: ServiceState
    mem_mb: float | None = None
    mem_limit_mb: float | None = None
    cpu_pct: float | None = None


class StageStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    p50_ms: float
    p95_ms: float


class BedrockStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    reachable: bool | None = None
    last_ttft_ms: float | None = None


class BackupStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: str | None = None
    ok: bool | None = None
    name: str | None = None
    destinations: list[str] = []
    local_only: bool = True
    restore_check: str | None = None


class HostStatus(BaseModel):
    """What ``host.json`` carries, plus how old it is."""

    model_config = ConfigDict(frozen=True)

    age_s: float | None = None
    stale: bool = True
    temperature_c: float | None = None
    throttled_raw: int | None = None
    throttled_now: list[str] = []
    throttled_since_boot: list[str] = []
    disk_free_bytes: int | None = None
    reboot_pending: bool | None = None
    chrony_offset_s: float | None = None
    raw: dict[str, Any] = {}


class HealthReport(BaseModel):
    """``GET /api/health`` details (admin only; the bare endpoint is just ``ok``)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    version: str
    schema_version: int
    services: list[ServiceStatus]
    audio_device_present: bool | None
    ha_connected: bool | None
    bedrock: BedrockStatus
    backup: BackupStatus
    cert_expires_at: datetime | None
    update_available: str | None
    stages_24h: dict[str, StageStats]
    host: HostStatus
    needs_attention: list[str]


@dataclass(frozen=True, slots=True)
class HealthProbes:
    """Live state owned by other builders; each returns ``None`` when unknown."""

    ha_connected: Callable[[], Awaitable[bool | None]] | None = None
    audio_device_present: Callable[[], Awaitable[bool | None]] | None = None
    bedrock: Callable[[], Awaitable[BedrockStatus]] | None = None
    update_available: Callable[[], Awaitable[str | None]] | None = None


async def _maybe[T](fn: Callable[[], Awaitable[T]] | None, default: T) -> T:
    return default if fn is None else await fn()


# ----------------------------------------------------------------- host.json --


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _bits(raw: int, shift: int = 0) -> list[str]:
    return [name for bit, name in THROTTLE_NOW.items() if raw & (bit << shift)]


def read_host_json(path: Path | None, now: datetime) -> tuple[HostStatus, list[ServiceStatus]]:
    """Parse ``host.json`` (schema in OPERATIONS §5); a missing file is all-``None``."""
    if path is None or not path.is_file():
        return HostStatus(), []
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return HostStatus(), []
    written = data.get("written_at")
    age: float | None = None
    if isinstance(written, str):
        try:
            age = (now - datetime.fromisoformat(written)).total_seconds()
        except ValueError:
            age = None
    throttled = data.get("throttled")
    raw = int(throttled, 16) if isinstance(throttled, str) else None
    services: list[ServiceStatus] = []
    containers = data.get("containers")
    if isinstance(containers, dict):
        for name, info in sorted(cast(dict[str, Any], containers).items()):
            if not isinstance(info, dict):
                continue
            c = cast(dict[str, Any], info)
            state = _container_state(str(c.get("state", "")), str(c.get("health", "")))
            services.append(
                ServiceStatus(
                    name=name,
                    state=state,
                    mem_mb=_num(c.get("mem_mb")),
                    mem_limit_mb=_num(c.get("mem_limit_mb")),
                    cpu_pct=_num(c.get("cpu_pct")),
                )
            )
    disk = data.get("disk_free_bytes")
    reboot = data.get("reboot_pending")
    host = HostStatus(
        age_s=age,
        stale=age is None or age > HOST_JSON_STALE.total_seconds(),
        temperature_c=_num(data.get("temperature_c")),
        throttled_raw=raw,
        throttled_now=_bits(raw) if raw is not None else [],
        throttled_since_boot=_bits(raw, THROTTLE_SINCE_BOOT_SHIFT) if raw is not None else [],
        disk_free_bytes=disk if isinstance(disk, int) else None,
        reboot_pending=reboot if isinstance(reboot, bool) else None,
        chrony_offset_s=_num(data.get("chrony_offset_s")),
        raw=data,
    )
    return host, services


def _container_state(state: str, health: str) -> ServiceState:
    if state != "running":
        return "down" if state else "unknown"
    return {"healthy": "ok", "starting": "starting", "unhealthy": "unhealthy"}.get(health, "ok")  # pyright: ignore[reportReturnType] - the dict values are all ServiceState


# ------------------------------------------------------------------- stages --


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100) of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def stage_stats(conn: sqlite3.Connection, since_ms: int) -> dict[str, StageStats]:
    """p50/p95 per stage over turns started since ``since_ms`` (OPERATIONS §11 table)."""
    samples: dict[str, list[float]] = {}
    rows = conn.execute(
        "SELECT stages_ms_json FROM turns WHERE created_at >= ?", (since_ms,)
    ).fetchall()
    for (raw,) in rows:
        try:
            stages = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(stages, dict):
            continue
        for name, value in cast(dict[str, object], stages).items():
            num = _num(value)
            if num is not None:
                samples.setdefault(name, []).append(num)
    return {
        name: StageStats(n=len(v), p50_ms=percentile(v, 50), p95_ms=percentile(v, 95))
        for name, v in sorted(samples.items())
    }


# ------------------------------------------------------------------- backup --


def backup_status(conn: sqlite3.Connection) -> BackupStatus:
    from beepboop.db.backup import LAST_CHECK_KEY, LAST_KEY

    last = repo.household.get(conn, LAST_KEY)
    check = repo.household.get(conn, LAST_CHECK_KEY)
    check_s = (
        str(cast(Mapping[str, object], check).get("outcome")) if isinstance(check, dict) else None
    )
    if not isinstance(last, dict):
        return BackupStatus(restore_check=check_s)
    d = cast(dict[str, Any], last)
    dests = d.get("destinations")
    names = (
        [str(cast(dict[str, Any], x).get("destination")) for x in cast(list[Any], dests)
         if isinstance(x, dict)]
        if isinstance(dests, list) else []
    )  # fmt: skip
    return BackupStatus(
        at=str(d.get("at")) if d.get("at") else None,
        ok=bool(d.get("ok")),
        name=str(d.get("name")) if d.get("name") else None,
        destinations=names,
        local_only=bool(d.get("local_only", True)),
        restore_check=check_s,
    )


def cert_expiry(path: Path | None) -> datetime | None:
    """``notAfter`` of the mounted Tier-0 root (``/caddy/root.crt``)."""
    if path is None or not path.is_file():
        return None
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except ValueError:
        return None
    return cert.not_valid_after_utc


# ------------------------------------------------------------------- report --


async def collect_health(
    db: Database,
    *,
    version: str,
    now: datetime,
    host_json: Path | None = None,
    ca_cert: Path | None = None,
    probes: HealthProbes | None = None,
) -> HealthReport:
    """Build the report. Cheap: one read job and a few small files."""
    p = probes or HealthProbes()
    since = to_ms(now - timedelta(hours=24))

    def _read(conn: sqlite3.Connection) -> tuple[dict[str, StageStats], BackupStatus]:
        return stage_stats(conn, since), backup_status(conn)

    stages, backup = await db.read(_read)
    host, services = read_host_json(host_json, now)
    ha = await _maybe(p.ha_connected, None)
    audio = await _maybe(p.audio_device_present, None)
    bedrock = await _maybe(p.bedrock, BedrockStatus())
    update = await _maybe(p.update_available, None)
    expires = cert_expiry(ca_cert)

    attention: list[str] = []
    attention += [f"{s.name} is {s.state}" for s in services if s.state in ("unhealthy", "down")]
    if host_json is not None and host.stale:
        attention.append("host status is stale (beepboop-host.timer not running?)")
    if host.throttled_now:
        attention.append("throttling: " + ", ".join(host.throttled_now))
    if host.reboot_pending:
        attention.append("reboot pending")
    if ha is False:
        attention.append("Home Assistant is not connected")
    if audio is False:
        attention.append("microphone array not detected")
    if backup.ok is False:
        attention.append("the last backup failed")
    elif backup.ok is None:
        attention.append("no backup yet")
    if backup.local_only:
        attention.append("Backups are only on this disk")
    if backup.restore_check == "fail":
        attention.append("the weekly restore check failed")
    if expires is not None and expires - now < timedelta(days=30):
        attention.append("the device certificate expires within 30 days")

    degraded = any(s.state in ("unhealthy", "down") for s in services) or ha is False
    return HealthReport(
        status="degraded" if degraded else "ok",
        version=version,
        schema_version=await db.schema_version(),
        services=services,
        audio_device_present=audio,
        ha_connected=ha,
        bedrock=bedrock,
        backup=backup,
        cert_expires_at=expires,
        update_available=update,
        stages_24h=stages,
        host=host,
        needs_attention=attention,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "BackupStatus",
    "BedrockStatus",
    "HealthProbes",
    "HealthReport",
    "HostStatus",
    "ServiceStatus",
    "StageStats",
    "backup_status",
    "cert_expiry",
    "collect_health",
    "percentile",
    "read_host_json",
    "stage_stats",
]
