"""``turns`` and ``tool_calls``: the audit trail (DATA_AND_PRIVACY §6.1, IDENTITY_AND_TRUST §11).

Trace writes (``durable=False``), coalesced by the writer. ``explain`` is the join the
Activity page and ``module check`` print ("why did you do that").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal, cast

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import dump_json, opt_float, opt_int, opt_str
from beepboop.policy.types import DenyReason, Factor, Outcome, Risk

TurnVia = Literal["voice", "web", "oauth", "pat", "system"]
TurnPath = Literal["fast", "llm", "offline", "error"]
ResultStatus = Literal["ok", "error", "denied", "pin_failed", "timeout", "dry_run", "cancelled"]
IdStage = Literal["provisional", "final"]


@dataclass(frozen=True, slots=True)
class TurnRow:
    id: str
    profile_id: str
    via: TurnVia
    locale: str
    path: TurnPath
    stt_provider: str
    tts_provider: str
    device: str = "main"
    actor_profile_id: str | None = None
    confidence: float | None = None
    margin: float | None = None
    id_stage: IdStage | None = None
    transcript: str | None = None
    reply: str | None = None
    stages_ms: dict[str, object] = field(default_factory=dict[str, object])
    tools_offered: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    cost_micro_usd: int = 0
    llm_provider: str | None = None
    tainted: bool = False
    corrected_profile_id: str | None = None
    flagged: bool = False
    false_wake: bool = False
    error: str | None = None
    created_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TurnRow:
        import json

        return cls(
            id=str(row["id"]),
            profile_id=str(row["profile_id"]),
            via=cast(TurnVia, row["via"]),
            locale=str(row["locale"]),
            path=cast(TurnPath, row["path"]),
            stt_provider=str(row["stt_provider"]),
            tts_provider=str(row["tts_provider"]),
            device=str(row["device"]),
            actor_profile_id=opt_str(row, "actor_profile_id"),
            confidence=opt_float(row, "confidence"),
            margin=opt_float(row, "margin"),
            id_stage=cast(IdStage | None, row["id_stage"]),
            transcript=opt_str(row, "transcript"),
            reply=opt_str(row, "reply"),
            stages_ms=cast(dict[str, object], json.loads(row["stages_ms_json"])),
            tools_offered=int(row["tools_offered"]),
            tokens_in=int(row["tokens_in"]),
            tokens_out=int(row["tokens_out"]),
            tokens_cached=int(row["tokens_cached"]),
            cost_micro_usd=int(row["cost_micro_usd"]),
            llm_provider=opt_str(row, "llm_provider"),
            tainted=bool(row["tainted"]),
            corrected_profile_id=opt_str(row, "corrected_profile_id"),
            flagged=bool(row["flagged"]),
            false_wake=bool(row["false_wake"]),
            error=opt_str(row, "error"),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class ToolCallRow:
    turn_id: str
    seq: int
    tool: str
    module_id: str
    args_redacted: dict[str, object]
    effective_risk: Risk
    decision: Outcome
    result_status: ResultStatus
    id: str = ""
    profile_id: str | None = None
    targets: list[dict[str, object]] | None = None
    deny_reason: DenyReason | None = None
    grant_id: str | None = None
    factor: Factor | None = None
    approval_id: str | None = None
    untrusted_output: bool = False
    duration_ms: int | None = None
    created_at: int = 0

    @property
    def is_safety_record(self) -> bool:
        """§9.3: critical calls and confirmed calls are kept, pseudonymised, for 365 days."""
        return self.effective_risk == "critical" or self.decision == "confirm"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ToolCallRow:
        import json

        targets_raw = opt_str(row, "targets_json")
        return cls(
            id=str(row["id"]),
            turn_id=str(row["turn_id"]),
            profile_id=opt_str(row, "profile_id"),
            seq=int(row["seq"]),
            tool=str(row["tool"]),
            module_id=str(row["module_id"]),
            args_redacted=cast(dict[str, object], json.loads(row["args_redacted_json"])),
            targets=None
            if targets_raw is None
            else cast(list[dict[str, object]], json.loads(targets_raw)),
            effective_risk=cast(Risk, row["effective_risk"]),
            decision=cast(Outcome, row["decision"]),
            deny_reason=cast(DenyReason | None, row["deny_reason"]),
            grant_id=opt_str(row, "grant_id"),
            factor=cast(Factor | None, row["factor"]),
            approval_id=opt_str(row, "approval_id"),
            untrusted_output=bool(row["untrusted_output"]),
            result_status=cast(ResultStatus, row["result_status"]),
            duration_ms=opt_int(row, "duration_ms"),
            created_at=int(row["created_at"]),
        )


def insert_turn(conn: sqlite3.Connection, t: TurnRow) -> None:
    conn.execute(
        "INSERT INTO turns (id, profile_id, actor_profile_id, via, device, confidence, margin,"
        " id_stage, locale, transcript, reply, path, stages_ms_json, tools_offered, tokens_in,"
        " tokens_out, tokens_cached, cost_micro_usd, llm_provider, stt_provider, tts_provider,"
        " tainted, corrected_profile_id, flagged, false_wake, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            t.id,
            t.profile_id,
            t.actor_profile_id,
            t.via,
            t.device,
            t.confidence,
            t.margin,
            t.id_stage,
            t.locale,
            t.transcript,
            t.reply,
            t.path,
            dump_json(t.stages_ms),
            t.tools_offered,
            t.tokens_in,
            t.tokens_out,
            t.tokens_cached,
            t.cost_micro_usd,
            t.llm_provider,
            t.stt_provider,
            t.tts_provider,
            int(t.tainted),
            t.corrected_profile_id,
            int(t.flagged),
            int(t.false_wake),
            t.error,
            t.created_at or now_ms(),
        ),
    )


def insert_tool_call(conn: sqlite3.Connection, c: ToolCallRow) -> str:
    call_id = c.id or new_ulid()
    conn.execute(
        "INSERT INTO tool_calls (id, turn_id, profile_id, seq, tool, module_id, args_redacted_json,"
        " targets_json, effective_risk, decision, deny_reason, grant_id, factor, approval_id,"
        " untrusted_output, result_status, duration_ms, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            c.turn_id,
            c.profile_id,
            c.seq,
            c.tool,
            c.module_id,
            dump_json(c.args_redacted),
            None if c.targets is None else dump_json(c.targets),
            c.effective_risk,
            c.decision,
            c.deny_reason,
            c.grant_id,
            c.factor,
            c.approval_id,
            int(c.untrusted_output),
            c.result_status,
            c.duration_ms,
            c.created_at or now_ms(),
        ),
    )
    return call_id


def get_turn(conn: sqlite3.Connection, turn_id: str) -> TurnRow | None:
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    return None if row is None else TurnRow.from_row(row)


def tool_calls_for(conn: sqlite3.Connection, turn_id: str) -> list[ToolCallRow]:
    rows = conn.execute(
        "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq", (turn_id,)
    ).fetchall()
    return [ToolCallRow.from_row(r) for r in rows]


def list_for_profile(
    conn: sqlite3.Connection, profile_id: str, *, limit: int = 50, before: int | None = None
) -> list[TurnRow]:
    """Newest first, cursor on ``created_at`` (WEB_UI §8 pagination)."""
    rows = conn.execute(
        "SELECT * FROM turns WHERE profile_id = ? AND (? IS NULL OR created_at < ?)"
        " ORDER BY created_at DESC LIMIT ?",
        (profile_id, before, before, limit),
    ).fetchall()
    return [TurnRow.from_row(r) for r in rows]


def last_turn_on_device(conn: sqlite3.Connection, device: str) -> TurnRow | None:
    row = conn.execute(
        "SELECT * FROM turns WHERE device = ? ORDER BY created_at DESC LIMIT 1", (device,)
    ).fetchone()
    return None if row is None else TurnRow.from_row(row)


def finish_turn(conn: sqlite3.Connection, t: TurnRow) -> None:
    """End-of-turn update of the row inserted at turn start (the agent writes both: the
    row must exist first because ``tool_calls`` rows reference it)."""
    conn.execute(
        "UPDATE turns SET transcript = ?, reply = ?, path = ?, stages_ms_json = ?,"
        " tools_offered = ?, tokens_in = ?, tokens_out = ?, tokens_cached = ?,"
        " cost_micro_usd = ?, llm_provider = ?, tainted = ?, false_wake = ?, error = ?"
        " WHERE id = ?",
        (
            t.transcript,
            t.reply,
            t.path,
            dump_json(t.stages_ms),
            t.tools_offered,
            t.tokens_in,
            t.tokens_out,
            t.tokens_cached,
            t.cost_micro_usd,
            t.llm_provider,
            int(t.tainted),
            int(t.false_wake),
            t.error,
            t.id,
        ),
    )


def previous_on_device(
    conn: sqlite3.Connection, device: str, before_turn_id: str
) -> TurnRow | None:
    """The newest turn on ``device`` other than ``before_turn_id`` ("that wasn't me",
    "forget that")."""
    row = conn.execute(
        "SELECT * FROM turns WHERE device = ? AND id != ? ORDER BY created_at DESC, id DESC"
        " LIMIT 1",
        (device, before_turn_id),
    ).fetchone()
    return None if row is None else TurnRow.from_row(row)


def set_flagged(conn: sqlite3.Connection, turn_id: str) -> None:
    """IDENTITY_AND_TRUST §11: float this turn to the top of the admin Activity view."""
    conn.execute("UPDATE turns SET flagged = 1 WHERE id = ?", (turn_id,))


def set_corrected(conn: sqlite3.Connection, turn_id: str, corrected_profile_id: str | None) -> None:
    """ "That wasn't me" (VOICE_PIPELINE §8.8): flag the turn and record the correction."""
    conn.execute(
        "UPDATE turns SET corrected_profile_id = ?, flagged = 1 WHERE id = ?",
        (corrected_profile_id, turn_id),
    )


def delete_turn(conn: sqlite3.Connection, turn_id: str) -> bool:
    """ "Delete this turn" (§9.3): blank transcript/reply, delete ordinary tool calls, keep safety
    records.
    """
    row = get_turn(conn, turn_id)
    if row is None:
        return False
    conn.execute("UPDATE turns SET transcript = NULL, reply = NULL WHERE id = ?", (turn_id,))
    conn.execute(
        "DELETE FROM tool_calls WHERE turn_id = ? AND NOT (effective_risk = 'critical' OR decision"
        " ="
        " 'confirm')",
        (turn_id,),
    )
    conn.execute(
        "UPDATE tool_calls SET profile_id = NULL WHERE turn_id = ?",
        (turn_id,),
    )
    return True


@dataclass(frozen=True, slots=True)
class Explanation:
    turn: TurnRow
    tool_calls: list[ToolCallRow]


def explain(conn: sqlite3.Connection, turn_id: str) -> Explanation | None:
    """``turns`` + ``tool_calls`` for one turn (IDENTITY_AND_TRUST §11 "why did you do that")."""
    turn = get_turn(conn, turn_id)
    if turn is None:
        return None
    return Explanation(turn, tool_calls_for(conn, turn_id))


def list_for_profiles(
    conn: sqlite3.Connection,
    profile_ids: list[str] | None,
    *,
    limit: int = 50,
    before: int | None = None,
) -> list[TurnRow]:
    """Newest first across several profiles (``None`` = everyone); cursor on ``created_at``."""
    if profile_ids is not None and not profile_ids:
        return []
    where = "(? IS NULL OR created_at < ?)"
    args: list[object] = [before, before]
    if profile_ids is not None:
        where += f" AND profile_id IN ({','.join('?' * len(profile_ids))})"
        args += profile_ids
    rows = conn.execute(
        f"SELECT * FROM turns WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",  # noqa: S608 - placeholders only
        (*args, limit),
    ).fetchall()
    return [TurnRow.from_row(r) for r in rows]


def mark_false_wake(conn: sqlite3.Connection, turn_id: str) -> bool:
    """ "Report false wake" (WEB_UI §5.8): flag the turn and drop its transcript."""
    return (
        conn.execute(
            "UPDATE turns SET false_wake = 1, transcript = NULL, reply = NULL WHERE id = ?",
            (turn_id,),
        ).rowcount
        > 0
    )
