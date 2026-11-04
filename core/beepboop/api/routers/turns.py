"""``/api/turns``: Activity, "why did you do that?" (WEB_UI §5.8, IDENTITY_AND_TRUST §10-§11).

Visibility (§3, §6.4): everyone sees their own turns; an admin also sees every child's
and guest's turn in full and other adults' tool calls *without* transcripts. A turn the
viewer may not see is a 404, the same as one that does not exist.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from beepboop.api.deps import ApiDeps, Deps, Session, WebSession, record_audit
from beepboop.api.problem import ApiError
from beepboop.db.repo import profiles, turns
from beepboop.db.repo.profiles import ProfileRow
from beepboop.db.repo.turns import ToolCallRow, TurnRow
from beepboop.db.retention import delete_turn

router = APIRouter(prefix="/api/turns", tags=["turns"])

Outcome = Literal[
    "fast path", "tool", "approved", "denied", "tainted", "error", "offline", "answer"
]
Visibility = Literal["full", "calls_only", "none"]


class ToolCallView(BaseModel):
    seq: int
    tool: str
    module_id: str
    args: dict[str, object]
    effective_risk: str
    decision: str
    deny_reason: str | None
    grant_id: str | None
    factor: str | None
    approval_id: str | None
    untrusted_output: bool
    result_status: str
    duration_ms: int | None


class TurnView(BaseModel):
    id: str
    created_at: int
    profile_id: str
    profile_name: str
    actor_profile_id: str | None
    via: str
    typed: bool
    device: str
    confidence: float | None
    margin: float | None
    id_stage: str | None
    transcript: str | None
    reply: str | None
    transcript_hidden: bool
    path: str
    outcome: Outcome
    tainted: bool
    flagged: bool
    false_wake: bool
    corrected_profile_id: str | None


class TurnDetail(TurnView):
    tool_calls: list[ToolCallView]
    stages_ms: dict[str, object]
    tools_offered: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cost_micro_usd: int
    llm_provider: str | None
    stt_provider: str
    tts_provider: str
    locale: str
    error: str | None


class TurnPage(BaseModel):
    items: list[TurnView]
    next_cursor: str | None


class NotMeBody(BaseModel):
    actual_profile_id: str | None = None


class NotMeResult(BaseModel):
    did: list[str]


def visibility(viewer: ProfileRow, subject: ProfileRow | None, turn: TurnRow) -> Visibility:
    if turn.profile_id == viewer.id or turn.actor_profile_id == viewer.id:
        return "full"
    if viewer.kind != "admin":
        return "none"
    if subject is None or subject.kind in ("child", "guest"):
        return "full"
    return "calls_only"


def outcome_of(t: TurnRow, calls: list[ToolCallRow], approved: set[str]) -> Outcome:
    if t.path == "error" or t.error:
        return "error"
    if t.path == "offline":
        return "offline"
    if any(c.decision == "deny" for c in calls):
        return "denied"
    confirms = [c for c in calls if c.approval_id is not None]
    if confirms:
        return "approved" if any(c.approval_id in approved for c in confirms) else "denied"
    if t.tainted:
        return "tainted"
    if t.path == "fast":
        return "fast path"
    return "tool" if calls else "answer"


def _view(
    t: TurnRow, vis: Visibility, name: str, calls: list[ToolCallRow], approved: set[str]
) -> TurnView:
    hidden = vis != "full"
    return TurnView(
        id=t.id,
        created_at=t.created_at,
        profile_id=t.profile_id,
        profile_name=name,
        actor_profile_id=t.actor_profile_id,
        via=t.via,
        typed=t.via == "web",
        device=t.device,
        confidence=t.confidence,
        margin=t.margin,
        id_stage=t.id_stage,
        transcript=None if hidden else t.transcript,
        reply=None if hidden else t.reply,
        transcript_hidden=hidden,
        path=t.path,
        outcome=outcome_of(t, calls, approved),
        tainted=t.tainted,
        flagged=t.flagged,
        false_wake=t.false_wake,
        corrected_profile_id=t.corrected_profile_id,
    )


def _call_view(c: ToolCallRow) -> ToolCallView:
    return ToolCallView(
        seq=c.seq,
        tool=c.tool,
        module_id=c.module_id,
        args=c.args_redacted,
        effective_risk=c.effective_risk,
        decision=c.decision,
        deny_reason=c.deny_reason,
        grant_id=c.grant_id,
        factor=c.factor,
        approval_id=c.approval_id,
        untrusted_output=c.untrusted_output,
        result_status=c.result_status,
        duration_ms=c.duration_ms,
    )


def _approved_ids(conn: sqlite3.Connection, calls: list[ToolCallRow]) -> set[str]:
    ids = [c.approval_id for c in calls if c.approval_id]
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT id FROM approvals WHERE decision = 'approved' AND id IN ({','.join('?' * len(ids))})",  # noqa: E501, S608 - placeholders only
        ids,
    ).fetchall()
    return {str(r[0]) for r in rows}


def _visible_profile_ids(
    conn: sqlite3.Connection, viewer: ProfileRow, who: str
) -> list[str] | None:
    if viewer.kind != "admin" or who == "me":
        return [viewer.id]
    if who == "everyone":
        return None
    return [who]


@router.get("", operation_id="turns_list")
async def turns_list(
    session: Session,
    deps: Deps,
    who: str = "me",
    outcome: Outcome | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> TurnPage:
    """Timeline, newest first. ``who``: ``me`` | ``everyone`` (admins) | a profile id."""
    viewer = session.profile
    before = int(cursor) if cursor and cursor.isdigit() else None

    def _q(c: sqlite3.Connection) -> TurnPage:
        ids = _visible_profile_ids(c, viewer, who)
        items: list[TurnView] = []
        names = {p.id: p for p in profiles.list_(c)}
        cur = before
        last: int | None = None
        while len(items) < limit:
            batch = turns.list_for_profiles(c, ids, limit=limit * 2, before=cur)
            if not batch:
                last = None
                break
            for t in batch:
                cur = t.created_at
                subject = names.get(t.profile_id)
                vis = visibility(viewer, subject, t)
                if vis == "none":
                    continue
                calls = turns.tool_calls_for(c, t.id)
                v = _view(
                    t,
                    vis,
                    subject.name if subject else "deleted user",
                    calls,
                    _approved_ids(c, calls),
                )
                if outcome is not None and v.outcome != outcome:
                    continue
                if q and (v.transcript is None or q.lower() not in v.transcript.lower()):
                    continue
                items.append(v)
                last = t.created_at
                if len(items) >= limit:
                    break
            if len(batch) < limit * 2:
                if len(items) < limit:
                    last = None
                break
        return TurnPage(items=items, next_cursor=None if last is None else str(last))

    return await deps.db.read(_q)


async def _load(deps: ApiDeps, session: WebSession, turn_id: str) -> tuple[TurnRow, Visibility]:
    def _q(c: sqlite3.Connection) -> tuple[TurnRow | None, ProfileRow | None]:
        t = turns.get_turn(c, turn_id)
        return t, None if t is None else profiles.get(c, t.profile_id)

    t, subject = await deps.db.read(_q)
    if t is None:
        raise ApiError(404, "not_found")
    vis = visibility(session.profile, subject, t)
    if vis == "none":
        raise ApiError(404, "not_found")
    return t, vis


@router.get("/{turn_id}", operation_id="turns_read")
async def turns_read(turn_id: str, session: Session, deps: Deps) -> TurnDetail:
    t, vis = await _load(deps, session, turn_id)

    def _q(c: sqlite3.Connection) -> TurnDetail:
        calls = turns.tool_calls_for(c, t.id)
        subject = profiles.get(c, t.profile_id)
        base = _view(
            t, vis, subject.name if subject else "deleted user", calls, _approved_ids(c, calls)
        )
        return TurnDetail(
            **base.model_dump(),
            tool_calls=[_call_view(x) for x in calls],
            stages_ms=t.stages_ms,
            tools_offered=t.tools_offered,
            tokens_in=t.tokens_in,
            tokens_out=t.tokens_out,
            tokens_cached=t.tokens_cached,
            cost_micro_usd=t.cost_micro_usd,
            llm_provider=t.llm_provider,
            stt_provider=t.stt_provider,
            tts_provider=t.tts_provider,
            locale=t.locale,
            error=t.error,
        )

    return await deps.db.read(_q)


def plain_words(call: ToolCallRow) -> str:
    """ "Turned off 2 garden lights"-style toast text from the audited call."""
    verb = call.tool.split(".", 1)[-1].replace("_", " ")
    what = ", ".join(str(v) for v in call.args_redacted.values() if isinstance(v, str | int))
    return f"{verb} {what}".strip().capitalize()


@router.post("/{turn_id}/not-me", operation_id="turns_not_me")
async def turns_not_me(turn_id: str, body: NotMeBody, session: Session, deps: Deps) -> NotMeResult:
    """*That wasn't me* (VOICE_PIPELINE §8.8): flags the turn; never undoes the action."""
    t, _vis = await _load(deps, session, turn_id)
    if not (session.is_admin or t.profile_id == session.profile.id):
        raise ApiError(403, "forbidden")
    if t.via != "voice":
        raise ApiError(409, "not_a_voice_turn")
    actual = body.actual_profile_id
    if actual is not None and await deps.db.read(lambda c: profiles.get(c, actual)) is None:
        raise ApiError(422, "unknown_profile", fields={"actual_profile_id": "unknown_profile"})
    await deps.db.write(lambda c: turns.set_corrected(c, t.id, actual), durable=True)
    calls = await deps.db.read(lambda c: turns.tool_calls_for(c, t.id))
    await record_audit(
        deps,
        session.profile,
        "turn.not_me",
        target=t.id,
        details={"attributed": t.profile_id, "actual": actual},
    )
    return NotMeResult(
        did=[plain_words(c) for c in calls if c.result_status == "ok" and c.decision == "allow"]
    )


@router.post("/{turn_id}/false-wake", operation_id="turns_false_wake", status_code=204)
async def turns_false_wake(turn_id: str, session: Session, deps: Deps) -> None:
    t, _vis = await _load(deps, session, turn_id)
    if not (session.is_admin or t.profile_id == session.profile.id):
        raise ApiError(403, "forbidden")
    await deps.db.write(lambda c: turns.mark_false_wake(c, t.id), durable=True)


@router.delete("/{turn_id}", operation_id="turns_delete", status_code=204)
async def turns_delete(turn_id: str, session: Session, deps: Deps) -> None:
    """*Delete this turn*: safety records stay, pseudonymised (DATA_AND_PRIVACY §9.3)."""
    t, _vis = await _load(deps, session, turn_id)
    if not (session.is_admin or t.profile_id == session.profile.id):
        raise ApiError(403, "forbidden")
    await deps.db.write(lambda c: delete_turn(c, t.id), durable=True)
    await record_audit(deps, session.profile, "turn.deleted", target=t.id)


__all__ = ["TurnDetail", "TurnView", "outcome_of", "router", "visibility"]
