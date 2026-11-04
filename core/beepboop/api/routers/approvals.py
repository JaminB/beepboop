"""``/api/approvals``: the web second factor (WEB_UI §5.7, IDENTITY_AND_TRUST §8).

Only approvals bound to the session's profile are visible (admins cannot approve for
others). Approve / deny / *That wasn't me* each need the nonce from the SSE ``approval``
event; the decision is the single guarded update of §8.2, so a second tap, a stale nonce,
another profile or an expired request gets ``409`` with a stable code. Execution (the
re-dispatch with the approval) belongs to whoever raised it (the turn loop) and is woken
through :meth:`EventBus.wait_decision`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from beepboop.api.deps import ApiDeps, Deps, Session, record_audit
from beepboop.api.events import iso
from beepboop.api.problem import ApiError
from beepboop.db.ids import to_ms
from beepboop.db.repo import approvals as approvals_repo
from beepboop.db.repo import turns
from beepboop.policy.approvals import ApprovalDecision, ApprovalRecord, TooManyPendingError

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

ApprovalState = Literal["pending", "approved", "denied", "expired"]


class ApprovalView(BaseModel):
    id: str
    tool: str
    summary: str
    state: ApprovalState
    created_at: str
    exp: str
    seconds_left: int
    factor: str | None = None
    executed: bool = False
    turn_id: str | None = None
    via: str | None = None
    device: str | None = None
    confidence: float | None = None


class NonceBody(BaseModel):
    nonce: str = Field(min_length=8, max_length=128)


def state_of(r: ApprovalRecord, deps: ApiDeps) -> ApprovalState:
    if r.decision is not None:
        return r.decision
    return "pending" if r.exp > deps.clock() else "expired"


async def view(deps: ApiDeps, r: ApprovalRecord) -> ApprovalView:
    turn = None
    if r.turn_id is not None:
        tid = r.turn_id
        turn = await deps.db.read(lambda c: turns.get_turn(c, tid))
    left = max(0, (to_ms(r.exp) - deps.now_ms()) // 1000) if r.decision is None else 0
    return ApprovalView(
        id=r.id,
        tool=r.tool,
        summary=r.summary,
        state=state_of(r, deps),
        created_at=iso(r.created_at),
        exp=iso(r.exp),
        seconds_left=int(left),
        factor=r.factor,
        executed=r.executed_at is not None,
        turn_id=r.turn_id,
        via=None if turn is None else turn.via,
        device=None if turn is None else turn.device,
        confidence=None if turn is None else turn.confidence,
    )


async def extend_pending_for(deps: ApiDeps, profile_id: str) -> list[ApprovalRecord]:
    """Extend each live approval for ``profile_id`` once to 120 s (§8.2), and publish."""
    now = deps.clock()
    service = deps.registry.approvals
    out: list[ApprovalRecord] = []
    for r in await service.store.pending_for(profile_id, now):
        if not r.extended and await service.extend_once(r.id, now):
            fresh = await service.store.get(r.id)
            if fresh is not None:
                deps.events.approval_extended(fresh)
                out.append(fresh)
                continue
        out.append(r)
    return out


async def expire_due(deps: ApiDeps) -> list[ApprovalRecord]:
    """Mark approvals past ``exp`` as ``expired`` and publish it, so the banner goes away
    and the waiting executor wakes to speak ``approval.expired`` (run on a short timer)."""
    now = deps.clock()
    await deps.db.write(lambda c: approvals_repo.expire_pending(c, now), durable=True)
    out: list[ApprovalRecord] = []
    for approval_id in deps.events.pending_ids():
        fresh = await deps.registry.approvals.store.get(approval_id)
        if fresh is not None and fresh.decision is not None:
            deps.events.approval_decided(fresh)
            out.append(fresh)
    return out


@router.get("", operation_id="approvals_list")
async def approvals_list(
    session: Session, deps: Deps, state: Literal["pending"] = "pending"
) -> list[ApprovalView]:
    """The polling fallback while the page is visible (§5.7); never carries the nonce."""
    rows = await deps.registry.approvals.store.pending_for(session.profile.id, deps.clock())
    return [await view(deps, r) for r in rows]


async def _own(deps: ApiDeps, approval_id: str, profile_id: str) -> ApprovalRecord:
    r = await deps.registry.approvals.store.get(approval_id)
    if r is None or r.profile_id != profile_id:
        raise ApiError(404, "not_found")
    return r


PRACTICE_TOOL = "practice"


@router.post("/practice", operation_id="approvals_practice", status_code=201)
async def approvals_practice(session: Session, deps: Deps) -> ApprovalView:
    """*Try it* at onboarding (WEB_UI §5.1): a no-op approval through the real banner path.

    Nothing executes it; approving or denying only records the decision.
    """
    if session.profile.kind not in ("admin", "member"):
        raise ApiError(403, "forbidden")
    try:
        record, _nonce = await deps.registry.approvals.create(
            session.principal, PRACTICE_TOOL, {}, [], "approve this practice request", deps.clock()
        )
    except TooManyPendingError as exc:
        raise ApiError(429, "too_many_pending", retry_after=60) from exc
    return await view(deps, record)


@router.get("/{approval_id}", operation_id="approvals_read")
async def approvals_read(approval_id: str, session: Session, deps: Deps) -> ApprovalView:
    return await view(deps, await _own(deps, approval_id, session.profile.id))


async def _decide(
    deps: ApiDeps,
    session_profile_id: str,
    approval_id: str,
    decision: ApprovalDecision,
    nonce: str,
) -> ApprovalRecord:
    result = await deps.registry.approvals.decide(
        approval_id, session_profile_id, decision, "web", deps.clock(), nonce=nonce
    )
    if isinstance(result, str):
        if result == "not_found":
            raise ApiError(404, "not_found")
        if result == "already_decided":
            # An expiry swept by expire_due() is a decision too; say what it was.
            current = await deps.registry.approvals.store.get(approval_id)
            if current is not None and current.decision == "expired":
                raise ApiError(409, "expired")
        raise ApiError(409, result)
    deps.events.approval_decided(result)
    return result


@router.post("/{approval_id}/approve", operation_id="approvals_approve")
async def approvals_approve(
    approval_id: str, body: NonceBody, session: Session, deps: Deps
) -> ApprovalView:
    r = await _decide(deps, session.profile.id, approval_id, "approved", body.nonce)
    return await view(deps, r)


@router.post("/{approval_id}/deny", operation_id="approvals_deny")
async def approvals_deny(
    approval_id: str, body: NonceBody, session: Session, deps: Deps
) -> ApprovalView:
    r = await _decide(deps, session.profile.id, approval_id, "denied", body.nonce)
    return await view(deps, r)


@router.post("/{approval_id}/not-me", operation_id="approvals_not_me")
async def approvals_not_me(
    approval_id: str, body: NonceBody, session: Session, deps: Deps
) -> ApprovalView:
    """Deny and record a misattribution (VOICE_PIPELINE §8.8)."""
    r = await _decide(deps, session.profile.id, approval_id, "denied", body.nonce)
    if r.turn_id is not None:
        tid = r.turn_id
        turn = await deps.db.read(lambda c: turns.get_turn(c, tid))
        # Only a voice turn attributed to this profile can be "not me"; a practice
        # approval's web turn (or any other profile's turn) is left alone.
        if turn is not None and turn.profile_id == session.profile.id and turn.via == "voice":
            await deps.db.write(lambda c: turns.set_corrected(c, tid, None))
    await record_audit(
        deps, session.profile, "approval.not_me", target=r.id, details={"turn_id": r.turn_id}
    )
    return await view(deps, r)


__all__ = ["ApprovalView", "expire_due", "extend_pending_for", "router"]
