"""``/api/system``: System > Health · LLM · Voice · Backups · Updates · HTTPS · Logs
(WEB_UI §5.11; OPERATIONS §9-§11). Admin only, except Health (members read it).

Numbers that only exist on the Pi (temperature, throttling, RAM per container) come from
``<data>/host.json`` when the host writes it and are ``null`` otherwise: the UI shows
"not measured", never a made-up value. Latency rows are p50/p95 over the last 24 h of
``turns.stages_ms_json``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from beepboop.api.deps import Admin, ApiDeps, Deps, Session, record_audit
from beepboop.api.events import iso
from beepboop.api.problem import ApiError
from beepboop.db.ids import from_ms
from beepboop.db.repo import household
from beepboop.secrets import SecretRef

router = APIRouter(prefix="/api/system", tags=["system"])

LATENCY_WINDOW = timedelta(hours=24)

#: System > LLM form (WEB_UI §9: core declares its schemas the same way modules do).
LLM_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "title": "Provider",
            "enum": ["bedrock"],
            "x-enum-labels": {"bedrock": "Amazon Bedrock"},
            "default": "bedrock",
            "x-order": 1,
        },
        "region": {
            "type": "string",
            "title": "Region",
            "default": "eu-central-1",
            "examples": ["eu-central-1"],
            "x-order": 2,
        },
        "model_id": {
            "type": "string",
            "title": "Model",
            "default": "global.anthropic.claude-haiku-5-5",
            "description": "Haiku 5.5 is the default; Sonnet 5.5 is opt-in.",
            "x-order": 3,
        },
        "effort": {
            "type": "string",
            "title": "Effort",
            "enum": ["low", "medium", "high"],
            "default": "low",
            "x-advanced": True,
            "x-order": 4,
        },
    },
    "required": ["region", "model_id"],
}
LLM_SECRETS = (
    {"name": "aws_access_key_id", "title": "AWS access key id"},
    {"name": "aws_secret_access_key", "title": "AWS secret access key"},
)
LLM_SECRET_OWNER = "core.llm"  # noqa: S105 - a module id, not a secret


class LatencyRow(BaseModel):
    stage: str
    p50_ms: float | None
    p95_ms: float | None
    n: int


class ModuleStatus(BaseModel):
    module_id: str
    loaded: bool


class SystemHealth(BaseModel):
    version: str
    schema_version: int
    modules: list[ModuleStatus]
    tools: int
    llm_provider: str | None
    audio: str
    ha: str
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    db_bytes: int | None
    wal_bytes: int | None
    last_backup_at: str | None
    host: dict[str, Any] | None
    latency: list[LatencyRow]


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(p * (len(s) - 1))))
    return round(s[k], 1)


def latency_rows(conn: sqlite3.Connection, since_ms: int) -> list[LatencyRow]:
    series: dict[str, list[float]] = {}
    for (raw,) in conn.execute(
        "SELECT stages_ms_json FROM turns WHERE created_at >= ?", (since_ms,)
    ).fetchall():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        for k, v in cast(dict[str, object], data).items():
            if isinstance(v, int | float):
                series.setdefault(k, []).append(float(v))
    return [
        LatencyRow(stage=k, p50_ms=_percentile(v, 0.5), p95_ms=_percentile(v, 0.95), n=len(v))
        for k, v in sorted(series.items())
    ]


def _read_host_json(deps: ApiDeps) -> dict[str, Any] | None:
    path = deps.host_json_path()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


async def _ha_word(deps: ApiDeps) -> str:
    probe = None if deps.health_probes is None else deps.health_probes.ha_connected
    if probe is None:
        return "unknown"
    connected = await probe()
    return "unknown" if connected is None else ("ok" if connected else "down")


@router.get("/health", operation_id="system_health")
async def system_health(session: Session, deps: Deps) -> SystemHealth:
    if session.profile.kind not in ("admin", "member"):
        raise ApiError(404, "not_found")
    since = deps.now_ms() - int(LATENCY_WINDOW.total_seconds() * 1000)
    latency = await deps.db.read(lambda c: latency_rows(c, since))
    disk_total = disk_free = db_bytes = wal_bytes = None
    if deps.data_dir is not None and deps.data_dir.is_dir():
        usage = shutil.disk_usage(deps.data_dir)
        disk_total, disk_free = usage.total, usage.free
        db = deps.data_dir / "beepboop.db"
        wal = deps.data_dir / "beepboop.db-wal"
        db_bytes = db.stat().st_size if db.is_file() else None
        wal_bytes = wal.stat().st_size if wal.is_file() else None
    backups = deps.backups.list() if deps.backups is not None else []
    last_backup = iso(from_ms(int(backups[0].stat().st_mtime * 1000))) if backups else None
    provider = None if deps.loop is None else getattr(deps.loop.provider, "id", None)
    return SystemHealth(
        version=deps.version,
        schema_version=await deps.db.schema_version(),
        modules=[ModuleStatus(module_id=m.id, loaded=True) for m, _ in deps.registry.modules()],
        tools=len(deps.registry.tools()),
        llm_provider=None if provider in (None, "none") else str(provider),
        audio="ok" if deps.enrollment.available else "offline",
        ha=await _ha_word(deps),
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
        db_bytes=db_bytes,
        wal_bytes=wal_bytes,
        last_backup_at=last_backup,
        host=_read_host_json(deps) if session.is_admin else None,
        latency=latency,
    )


# ------------------------------------------------------------------ updates --


class UpdatesView(BaseModel):
    current: str
    available: str | None
    changelog_url: str | None
    command: str = "beepboop update"
    notify_only: bool = True


@router.get("/updates", operation_id="system_updates")
async def system_updates(_admin: Admin, deps: Deps) -> UpdatesView:
    """Notify-only in v0.1 (OPERATIONS §9): the host command does the work."""
    avail = await deps.updates.available()
    return UpdatesView(
        current=deps.version,
        available=None if avail is None else avail.get("version"),
        changelog_url=None if avail is None else avail.get("changelog_url"),
    )


# ------------------------------------------------------------------ backups --


class BackupItem(BaseModel):
    name: str
    bytes: int
    at: str


class BackupsView(BaseModel):
    destinations: list[str]
    only_local: bool
    schedule: str
    backups: list[BackupItem]
    encrypted: bool


def _backups(deps: ApiDeps) -> BackupsView:
    items: list[BackupItem] = []
    if deps.backups is not None:
        for p in deps.backups.list():
            st = p.stat()
            items.append(
                BackupItem(name=p.name, bytes=st.st_size, at=iso(from_ms(int(st.st_mtime * 1000))))
            )
    return BackupsView(
        destinations=["this disk"],
        only_local=True,
        schedule="nightly 03:00",
        backups=items,
        encrypted=False,
    )


@router.get("/backups", operation_id="system_backups")
async def system_backups(_admin: Admin, deps: Deps) -> BackupsView:
    return _backups(deps)


@router.post("/backups", operation_id="system_backup_now")
async def system_backup_now(admin: Admin, deps: Deps) -> BackupsView:
    if deps.backups is None:
        raise ApiError(503, "backups_unavailable")
    path = await deps.backups.run(deps.clock())
    await record_audit(deps, admin.profile, "backup.created", target=path.name)
    return _backups(deps)


# ---------------------------------------------------------------------- LLM --


class SecretState(BaseModel):
    name: str
    title: str
    set: bool


class LlmView(BaseModel):
    settings_schema: dict[str, Any]
    values: dict[str, Any]
    secrets: list[SecretState]
    provider_active: str | None


class LlmBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict[str, Any])
    secrets: dict[str, str] = Field(default_factory=dict[str, str])


async def _llm(deps: ApiDeps) -> LlmView:
    stored = await deps.db.read(lambda c: household.get(c, "llm"))
    values = cast(dict[str, Any], stored) if isinstance(stored, dict) else {}
    states: list[SecretState] = []
    for s in LLM_SECRETS:
        present = deps.secrets is not None and await deps.secrets.exists(
            SecretRef(s["name"], owner_module=LLM_SECRET_OWNER)
        )
        states.append(SecretState(name=s["name"], title=s["title"], set=present))
    provider = None if deps.loop is None else getattr(deps.loop.provider, "id", None)
    return LlmView(
        settings_schema=LLM_SETTINGS_SCHEMA,
        values=values,
        secrets=states,
        provider_active=None if provider in (None, "none") else str(provider),
    )


@router.get("/llm", operation_id="system_llm")
async def system_llm(_admin: Admin, deps: Deps) -> LlmView:
    return await _llm(deps)


@router.put("/llm", operation_id="system_llm_save")
async def system_llm_save(body: LlmBody, admin: Admin, deps: Deps) -> LlmView:
    from beepboop.api.routers.integrations import validate_settings

    errors = validate_settings(LLM_SETTINGS_SCHEMA, body.values)
    names = {s["name"] for s in LLM_SECRETS}
    for k in body.secrets:
        if k not in names:
            errors[k] = "unknown_secret"
    if errors:
        raise ApiError(422, "invalid", fields=errors)
    if body.secrets:
        if deps.secrets is None:
            raise ApiError(503, "secrets_unavailable")
        for k, v in body.secrets.items():
            if v:
                await deps.secrets.put(SecretRef(k, owner_module=LLM_SECRET_OWNER), v)
    await deps.db.write(lambda c: household.set_(c, "llm", body.values), durable=True)
    await record_audit(deps, admin.profile, "llm.settings_saved")
    return await _llm(deps)


# -------------------------------------------------------------------- HTTPS --


class HttpsView(BaseModel):
    tier: int
    canonical_url: str
    fingerprint: str | None
    admin_gates: list[dict[str, str]]


def ca_fingerprint(deps: ApiDeps) -> str | None:
    """First 6 byte-groups of the root certificate's SHA-256 (WEB_UI §7)."""
    return fingerprint_of(deps.ca_cert)


def fingerprint_of(cert_path: Path | None) -> str | None:
    """The spoken/printed fingerprint of a PEM or DER certificate file; ``None`` if absent."""
    if cert_path is None or not cert_path.is_file():
        return None
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    raw = cert_path.read_bytes()
    cert = (
        x509.load_pem_x509_certificate(raw)
        if b"BEGIN" in raw
        else x509.load_der_x509_certificate(raw)
    )
    digest = cert.fingerprint(hashes.SHA256())
    return " ".join(f"{b:02X}" for b in digest[:6])


@router.get("/https", operation_id="system_https")
async def system_https(_admin: Admin, deps: Deps) -> HttpsView:
    return HttpsView(
        tier=0,
        canonical_url=deps.public_url,
        fingerprint=ca_fingerprint(deps),
        admin_gates=[
            {"name": "Home Assistant UI", "port": "8443"},
            {"name": "Music Assistant UI", "port": "8445"},
        ],
    )


# --------------------------------------------------------------------- Logs --


@router.get("/logs", operation_id="system_logs")
async def system_logs(
    _admin: Admin,
    deps: Deps,
    turn_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=500),
) -> list[dict[str, Any]]:
    """The last 500 structlog lines (already redacted), newest last."""
    if deps.logs is None:
        return []
    lines = deps.logs.lines(limit)
    if turn_id:
        lines = [ln for ln in lines if ln.get("turn_id") == turn_id]
    return [json.loads(json.dumps(ln, default=str)) for ln in lines]


__all__ = ["LLM_SETTINGS_SCHEMA", "ca_fingerprint", "fingerprint_of", "latency_rows", "router"]


# ------------------------------------------------------------------- voice --


class VoiceThresholds(BaseModel):
    """VOICE_PIPELINE §8.4 household thresholds; per-profile calibration overrides them."""

    tau_act: float = Field(ge=0.3, le=0.99)
    margin_act: float = Field(ge=0.0, le=0.5)
    tau_personal: float = Field(ge=0.3, le=0.99)
    margin_personal: float = Field(ge=0.0, le=0.5)


class VoiceView(BaseModel):
    wake_word: str = "Hey beepboop"
    #: Set in ``/config/beepboop.yaml`` (hard rule 4: provider configuration reaches the
    #: audio service as a mounted file, never as an event); shown, not edited, in v0.1.
    wake_sensitivity: float | None = None
    false_wakes_7d: int
    thresholds: VoiceThresholds
    ask_when_unsure: bool
    stt: str
    tts: str
    cloud_speech: bool = False
    #: Pairs of people who sound alike (the enrollment similarity warning); empty until the
    #: audio bridge computes it (ROADMAP v0.1 gap).
    similar: list[list[str]] = Field(default_factory=list[list[str]])


class VoicePatch(BaseModel):
    thresholds: VoiceThresholds | None = None
    ask_when_unsure: bool | None = None


async def _voice_view(deps: ApiDeps) -> VoiceView:
    from beepboop.db.repo.policy_view import household_calibration

    since = deps.now_ms() - 7 * 24 * 3600 * 1000

    def _q(c: sqlite3.Connection) -> tuple[VoiceThresholds, bool, int, object]:
        cal = household_calibration(c)
        ask = household.get(c, "voice.ask_when_unsure")
        row = c.execute(
            "SELECT COUNT(*) FROM turns WHERE false_wake = 1 AND created_at >= ?", (since,)
        ).fetchone()
        return (
            VoiceThresholds(
                tau_act=cal.tau_act,
                margin_act=cal.margin_act,
                tau_personal=cal.tau_personal,
                margin_personal=cal.margin_personal,
            ),
            True if ask is None else bool(ask),
            0 if row is None else int(row[0]),
            household.get(c, "voice.wake_sensitivity"),
        )

    thresholds, ask, false_wakes, sensitivity = await deps.db.read(_q)
    return VoiceView(
        wake_sensitivity=float(sensitivity) if isinstance(sensitivity, int | float) else None,
        false_wakes_7d=false_wakes,
        thresholds=thresholds,
        ask_when_unsure=ask,
        stt="sherpa-zipformer",
        tts="sherpa-piper",
    )


@router.get("/voice", operation_id="system_voice")
async def system_voice(_admin: Admin, deps: Deps) -> VoiceView:
    """System > Voice (WEB_UI §5.11): wake word, household thresholds, false wakes."""
    return await _voice_view(deps)


@router.patch("/voice", operation_id="system_voice_update")
async def system_voice_update(body: VoicePatch, admin: Admin, deps: Deps) -> VoiceView:
    """Household thresholds and "ask on phone when unsure"; audited, policy reloaded."""
    changed: dict[str, object] = {}
    if body.thresholds is not None:
        t = body.thresholds
        if t.tau_personal < t.tau_act:
            raise ApiError(422, "invalid", fields={"thresholds.tau_personal": "below_tau_act"})
        changed.update(
            {
                "voice.tau_act": t.tau_act,
                "voice.margin_act": t.margin_act,
                "voice.tau_personal": t.tau_personal,
                "voice.margin_personal": t.margin_personal,
            }
        )
    if body.ask_when_unsure is not None:
        changed["voice.ask_when_unsure"] = body.ask_when_unsure

    def _w(c: sqlite3.Connection) -> None:
        for k, v in changed.items():
            household.set_(c, k, v)

    if changed:
        await deps.db.write(_w, durable=True)
        await deps.refresh_policy()
        await record_audit(deps, admin.profile, "voice.updated", details={"keys": sorted(changed)})
    return await _voice_view(deps)
