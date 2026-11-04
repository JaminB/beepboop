"""``/api/privacy``: the ledger, export and "delete all transcripts" (WEB_UI §5.10,
DATA_AND_PRIVACY §9, §10).

The ledger is derived, not a separate log: counts from ``turns``, ``voiceprints``,
``secrets``, ``integrations``, ``sessions``, ``kv`` and the modules' ``egress``
declarations. The export is the §9.2 zip, written to ``<data>/export/``, downloadable
once within one hour, then deleted. No embeddings, hashes or secrets are ever exported.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from beepboop.api.deps import ApiDeps, Deps, Session, record_audit
from beepboop.api.problem import ApiError
from beepboop.api.routers.profiles import load_visible
from beepboop.api.security import new_token
from beepboop.db.ids import to_ms
from beepboop.db.repo import household, logins, profiles
from beepboop.db.repo.profiles import ProfileRow

router = APIRouter(prefix="/api/privacy", tags=["privacy"])

EXPORT_TTL = timedelta(hours=1)
LEDGER_WINDOW = timedelta(days=30)


class KeptItem(BaseModel):
    key: str
    count: int
    detail: str


class LeftItem(BaseModel):
    destination: str
    module_id: str | None
    count: int
    data: str | None
    purpose: str


class Ledger(BaseModel):
    profile_id: str
    kept: list[KeptItem]
    left: list[LeftItem]
    transcript_retention_days: int
    audio_kept: bool = False


class ExportReady(BaseModel):
    export_id: str
    filename: str
    expires_at: int


def _count(conn: sqlite3.Connection, sql: str, *args: object) -> int:
    row = conn.execute(sql, args).fetchone()
    return 0 if row is None else int(row[0])


@router.get("/{profile_id}", operation_id="privacy_ledger")
async def privacy_ledger(profile_id: str, session: Session, deps: Deps) -> Ledger:
    target = await load_visible(deps, session, profile_id)
    since = deps.now_ms() - int(LEDGER_WINDOW.total_seconds() * 1000)
    egress = {m.id: m.egress for m, _ in deps.registry.modules() if m.egress}

    def _q(c: sqlite3.Connection) -> Ledger:
        pid = target.id
        retention = (
            int(str(household.get(c, "transcript_retention_days") or 30))
            if target.transcript_retention_days is None
            else target.transcript_retention_days
        )
        kept = [
            KeptItem(
                key="voiceprint",
                count=_count(c, "SELECT COUNT(*) FROM voiceprints WHERE profile_id = ?", pid),
                detail="encrypted · no recordings",
            ),
            KeptItem(
                key="transcripts",
                count=_count(
                    c,
                    "SELECT COUNT(*) FROM turns WHERE profile_id = ? AND transcript IS NOT NULL",
                    pid,
                ),
                detail=f"kept {retention} days",
            ),
            KeptItem(
                key="secrets",
                count=_count(c, "SELECT COUNT(*) FROM secrets WHERE profile_id = ?", pid),
                detail="encrypted",
            ),
            KeptItem(
                key="module_data",
                count=_count(c, "SELECT COUNT(*) FROM kv WHERE profile_id = ?", pid),
                detail="timers, lists and other module data",
            ),
            KeptItem(
                key="sessions",
                count=_count(
                    c, "SELECT COUNT(*) FROM sessions WHERE profile_id = ? AND kind = 'web'", pid
                ),
                detail="signed-in browsers",
            ),
            KeptItem(key="audio", count=0, detail="none"),
        ]
        left: list[LeftItem] = []
        for prov, n in c.execute(
            "SELECT llm_provider, COUNT(*) FROM turns WHERE profile_id = ? AND path = 'llm'"
            " AND created_at >= ? AND llm_provider IS NOT NULL GROUP BY llm_provider",
            (pid, since),
        ).fetchall():
            left.append(
                LeftItem(
                    destination=str(prov),
                    module_id=None,
                    count=int(n),
                    data="transcripts and tool results",
                    purpose="answering requests",
                )
            )
        for module_id, decls in sorted(egress.items()):
            n = _count(
                c,
                "SELECT COUNT(*) FROM tool_calls WHERE profile_id = ? AND module_id = ?"
                " AND created_at >= ? AND result_status = 'ok'",
                pid,
                module_id,
                since,
            )
            for d in decls:
                left.append(
                    LeftItem(
                        destination=d.destination,
                        module_id=module_id,
                        count=n,
                        data=d.data,
                        purpose=d.purpose,
                    )
                )
        return Ledger(profile_id=pid, kept=kept, left=left, transcript_retention_days=retention)

    return await deps.db.read(_q)


@router.delete("/{profile_id}/transcripts", operation_id="privacy_delete_transcripts")
async def privacy_delete_transcripts(profile_id: str, session: Session, deps: Deps) -> int:
    """*Delete all* transcripts: blank ``transcript`` and ``reply`` on every turn."""
    target = await load_visible(deps, session, profile_id)
    if not (session.is_admin or session.profile.id == target.id):
        raise ApiError(403, "forbidden")
    n = await deps.db.write(
        lambda c: (
            c.execute(
                "UPDATE turns SET transcript = NULL, reply = NULL WHERE profile_id = ?"
                " AND (transcript IS NOT NULL OR reply IS NOT NULL)",
                (target.id,),
            ).rowcount
        ),
        durable=True,
    )
    await record_audit(deps, session.profile, "privacy.transcripts_deleted", target=target.id)
    return int(n)


def _rows(conn: sqlite3.Connection, sql: str, *args: object) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def build_export(
    conn: sqlite3.Connection, target: ProfileRow, now: datetime, *, words: bool = True
) -> bytes:
    """The DATA_AND_PRIVACY §9.2 zip (no hashes, no embeddings, no secret values).
    ``words=False`` leaves ``transcript``/``reply`` out of ``turns.jsonl`` (an admin
    exporting another adult, WEB_UI §3)."""
    pid = target.id
    login = logins.get(conn, pid)
    profile = {
        k: v
        for k, v in _rows(conn, "SELECT * FROM profiles WHERE id = ?", pid)[0].items()
        if k != "pin_hash_enc"
    }
    profile["pin_set"] = target.pin_hash_enc is not None
    profile["login"] = (
        None
        if login is None
        else {"username": login.username, "totp": login.totp_secret_enc is not None}
    )
    profile["groups"] = sorted(profiles.groups_for(conn, pid))
    groups = profile["groups"]
    grant_rows = _rows(
        conn, "SELECT * FROM grants WHERE subject_type = 'profile' AND subject_id = ?", pid
    )
    for g in groups:
        grant_rows += _rows(
            conn, "SELECT * FROM grants WHERE subject_type = 'group' AND subject_id = ?", g
        )
    for g in grant_rows:
        if g["resource_type"] == "ha_entity":
            ent = conn.execute(
                "SELECT friendly_name, entity_id FROM ha_entities WHERE uuid = ?",
                (g["resource_id"],),
            ).fetchone()
            g["resource_name"] = None if ent is None else (ent[0] or ent[1])
    turns_lines: list[str] = []
    for t in _rows(conn, "SELECT * FROM turns WHERE profile_id = ? ORDER BY created_at", pid):
        if not words:
            t["transcript"] = None
            t["reply"] = None
        t["tool_calls"] = _rows(
            conn, "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq", t["id"]
        )
        turns_lines.append(json.dumps(t, sort_keys=True, default=str))
    approvals = _rows(
        conn,
        "SELECT id, turn_id, tool, summary, created_at, exp, decision,"
        " decided_at, executed_at, factor FROM approvals WHERE profile_id = ?",
        pid,
    )
    turns_lines += [json.dumps({"approval": a}, sort_keys=True) for a in approvals]
    modules = _rows(
        conn, "SELECT module_id, key, value_json, updated_at FROM kv WHERE profile_id = ?", pid
    )
    integrations = _rows(
        conn,
        "SELECT module_id, settings_json, status, updated_at FROM"
        " integrations WHERE profile_id = ?",
        pid,
    )
    for i in integrations:
        names = conn.execute(
            "SELECT name FROM secrets WHERE owner_module = ? AND profile_id = ?",
            (i["module_id"], pid),
        ).fetchall()
        i["secrets"] = {str(n[0]): {"set": True} for n in names}
    voice = _rows(
        conn,
        "SELECT model_id, model_sha, dim, k, n_clips, calib_json, consent_at,"
        " consent_version, consented_by, updated_at FROM voiceprints WHERE"
        " profile_id = ?",
        pid,
    )
    audit_lines = [
        json.dumps(a, sort_keys=True)
        for a in _rows(conn, "SELECT * FROM audit WHERE actor_id = ? ORDER BY at", pid)
    ]
    retention = (
        household.get(conn, "transcript_retention_days") or 30
        if target.transcript_retention_days is None
        else target.transcript_retention_days
    )
    readme = (
        f"beepboop export for {target.name}, made {now.astimezone(UTC).isoformat()}.\n\n"
        "profile.json       your profile, sign-in (username, TOTP on/off; no hashes), groups\n"
        "grants.json        what you may use, with Home Assistant names\n"
        + (
            "turns.jsonl        your requests, the tool calls they made, and your approvals\n"
            if words
            else "turns.jsonl        tool calls and approvals; transcripts and replies are\n"
            "                   left out of an export an admin makes for another adult\n"
        )
        + "modules.json       your timers, lists and other module data\n"
        "integrations.json  your connections' settings; secrets are listed by name only\n"
        "voiceprint.json    voiceprint metadata only. No embeddings: they would let anyone\n"
        "                   impersonate you. No recordings exist.\n"
        "audit.jsonl        security-relevant actions you took\n\n"
        f"Transcripts are kept {retention} days.\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("profile.json", json.dumps(profile, indent=2, sort_keys=True, default=str))
        z.writestr("grants.json", json.dumps(grant_rows, indent=2, sort_keys=True))
        z.writestr("turns.jsonl", "\n".join(turns_lines) + ("\n" if turns_lines else ""))
        z.writestr("modules.json", json.dumps(modules, indent=2, sort_keys=True))
        z.writestr("integrations.json", json.dumps(integrations, indent=2, sort_keys=True))
        z.writestr("voiceprint.json", json.dumps(voice, indent=2, sort_keys=True))
        z.writestr("audit.jsonl", "\n".join(audit_lines) + ("\n" if audit_lines else ""))
        z.writestr("README.txt", readme)
    return buf.getvalue()


def _export_dir(deps: ApiDeps) -> Path:
    if deps.data_dir is None:
        raise ApiError(503, "no_data_dir")
    d = deps.data_dir / "export"
    d.mkdir(parents=True, exist_ok=True)
    return d


def purge_exports(directory: Path, now: datetime) -> int:
    n = 0
    for f in directory.glob("*.zip"):
        if now.timestamp() - f.stat().st_mtime >= EXPORT_TTL.total_seconds():
            f.unlink(missing_ok=True)
            n += 1
    return n


@router.post("/{profile_id}/export", operation_id="privacy_export")
async def privacy_export(profile_id: str, session: Session, deps: Deps) -> ExportReady:
    target = await load_visible(deps, session, profile_id)
    if not (session.is_admin or session.profile.id == target.id):
        raise ApiError(403, "forbidden")
    now = deps.clock()
    # WEB_UI §3 / DATA_AND_PRIVACY §9.2: an admin sees other adults' tool calls without
    # transcripts, so their export of another adult carries none either.
    words = target.id == session.profile.id or target.kind in ("child", "guest")
    data = await deps.db.read(lambda c: build_export(c, target, now, words=words))
    directory = _export_dir(deps)
    purge_exports(directory, now)
    export_id = new_token()[:24].replace("-", "x").replace("_", "y")
    safe = "".join(ch for ch in target.name if ch.isalnum()) or "profile"
    filename = f"beepboop-export-{safe}-{now.astimezone(UTC):%Y-%m-%d}.zip"
    (directory / f"{target.id}.{export_id}.zip").write_bytes(data)
    await record_audit(deps, session.profile, "privacy.exported", target=target.id)
    return ExportReady(
        export_id=export_id,
        filename=filename,
        expires_at=to_ms(now + EXPORT_TTL),
    )


@router.get(
    "/{profile_id}/export/{export_id}",
    operation_id="privacy_export_download",
    response_class=Response,
)
async def privacy_export_download(
    profile_id: str, export_id: str, session: Session, deps: Deps
) -> Response:
    target = await load_visible(deps, session, profile_id)
    if not export_id.isalnum():
        raise ApiError(404, "not_found")
    path = _export_dir(deps) / f"{target.id}.{export_id}.zip"
    if not path.is_file():
        raise ApiError(404, "not_found")
    if deps.clock().timestamp() - path.stat().st_mtime >= EXPORT_TTL.total_seconds():
        path.unlink(missing_ok=True)
        raise ApiError(410, "export_expired")
    data = path.read_bytes()
    path.unlink(missing_ok=True)  # once
    safe = "".join(ch for ch in target.name if ch.isalnum()) or "profile"
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="beepboop-export-{safe}.zip"'},
    )


__all__ = ["build_export", "router"]
