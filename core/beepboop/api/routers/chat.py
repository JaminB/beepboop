"""``POST /api/chat``: Activity > *Try a request* (WEB_UI §5.9).

Same agent loop, policy path and ``turns`` table as voice, with ``via: web`` and
``confidence: None``. *Test as* is admin-only and only for child or guest profiles: the
principal carries ``actor_profile_id = <admin>``, ``personal`` and ``critical`` tools run
in forced dry-run, and any approval is routed to the acting admin (``ApprovalService``
does that). *Dry-run* (default on) asks every module to log intended effects instead of
executing them, exactly like ``beepboop module check``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from beepboop.agent.turn import ListSpeaker, TurnLoop
from beepboop.agent.types import Speech, TurnResult, Utterance
from beepboop.api.deps import Deps, Session
from beepboop.api.problem import ApiError
from beepboop.db.ids import new_ulid
from beepboop.db.repo import profiles
from beepboop.module.context import ToolContext
from beepboop.policy.approvals import ApprovalRecord
from beepboop.policy.authorize import effective_risk
from beepboop.policy.types import Approval, Principal, ProfileKind
from beepboop.registry.registry import ContextFactory, DispatchResult, RegisteredTool, Registry

router = APIRouter(prefix="/api/chat", tags=["chat"])

FORCED_DRY_RUN_TIERS = ("personal", "critical")


@dataclass(frozen=True, slots=True)
class _DryRunContexts:
    """``Registry.dispatch(dry_run=True)`` labels the audit row but does not tell the
    context; this wrapper sets ``ctx.dry_run`` for the forced calls (MODULES §12.3)."""

    inner: ContextFactory
    force: Callable[[str], bool]

    def for_call(
        self,
        tool: RegisteredTool,
        principal: Principal,
        *,
        deadline: float | None,
        approval: Approval | None,
    ) -> ToolContext:
        ctx = self.inner.for_call(tool, principal, deadline=deadline, approval=approval)
        return replace(ctx, dry_run=True) if self.force(tool.name) else ctx


@dataclass
class DryRunRegistry(Registry):
    """The live registry's tools and policy, with ``dry_run`` forced for some calls."""

    force: Callable[[str], bool] = lambda _tool: True

    @classmethod
    def wrap(cls, registry: Registry, force: Callable[[str], bool]) -> DryRunRegistry:
        kwargs: dict[str, Any] = {f.name: getattr(registry, f.name) for f in fields(registry)}
        kwargs["context_factory"] = _DryRunContexts(registry.context_factory, force)
        return cls(**kwargs, force=force)

    async def dispatch(
        self,
        principal: Principal,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        approval: ApprovalRecord | None = None,
        dry_run: bool = False,
    ) -> DispatchResult:
        forced = self.force(tool_name)
        if not forced and principal.actor_profile_id is not None:
            # IDENTITY_AND_TRUST §3.1: *test as* forces personal/critical **calls** to dry-run,
            # so the effective tier (an act tool unlocking a lock) decides, not the declared one.
            try:
                tool = self.get(tool_name)
                targets = await self.targets(principal, tool, args)
                risk = effective_risk(tool, targets, self.policy_state().services)
                forced = risk in FORCED_DRY_RUN_TIERS
            except KeyError:
                forced = True
        return await super().dispatch(
            principal,
            tool_name,
            args,
            approval=approval,
            dry_run=dry_run or forced,
        )


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    as_profile_id: str | None = None
    dry_run: bool = True
    device: str = Field(default="main", max_length=32)


class ToolCallChip(BaseModel):
    tool: str
    decision: str
    reason: str | None
    risk: str
    dry_run: bool


class ChatReply(BaseModel):
    turn_id: str
    path: str
    reply: str
    tool_calls: list[ToolCallChip]
    pending_approval_id: str | None
    as_profile_id: str
    dry_run: bool


def speech_text(s: Speech) -> str:
    if s.text:
        return s.text
    if s.clip is None:
        return ""
    from beepboop.agent.phrases import PHRASES, placeholders

    text = PHRASES.get(s.clip)
    if text is None or placeholders(s.clip):
        return s.clip
    return text


_FOLLOWERS: set[asyncio.Task[None]] = set()


def _follow_in_background(
    deps: Deps,
    loop: TurnLoop,
    result: TurnResult,
    utterance: Utterance,
    profile_id: str,
    kind: ProfileKind,
) -> None:
    async def _run() -> None:
        before = len(result.speech)
        try:
            await loop.follow_approval(result, utterance)
        finally:
            spoken = " ".join(t for t in (speech_text(s) for s in result.speech[before:]) if t)
            deps.events.publish(
                "turn",
                {
                    "id": result.turn_id,
                    "profile_id": profile_id,
                    "state": "done",
                    "reply_delta": spoken,
                    "approval_id": result.pending_approval_id,
                },
                audience="turn",
                subject_profile_id=profile_id,
                subject_kind=kind,
            )

    task = asyncio.create_task(_run(), name=f"chat.follow.{result.turn_id}")
    _FOLLOWERS.add(task)
    task.add_done_callback(_FOLLOWERS.discard)


@router.post("", operation_id="chat_send")
async def chat_send(body: ChatBody, session: Session, deps: Deps) -> ChatReply:
    if deps.loop is None:
        raise ApiError(503, "agent_unavailable")
    if session.profile.kind == "guest":
        raise ApiError(403, "forbidden")
    target = session.profile
    actor: str | None = None
    if body.as_profile_id and body.as_profile_id != session.profile.id:
        if not session.is_admin:
            raise ApiError(403, "test_as_admin_only")
        found = await deps.db.read(lambda c: profiles.get(c, body.as_profile_id or ""))
        if found is None or found.kind not in ("child", "guest"):
            raise ApiError(
                422, "test_as_child_or_guest", fields={"as_profile_id": "child_or_guest"}
            )
        target, actor = found, session.profile.id
    turn_id = new_ulid()
    principal = replace(
        session.principal,
        profile_id=target.id,
        actor_profile_id=actor,
        turn_id=turn_id,
    )
    registry = deps.registry

    def force(tool: str) -> bool:
        if body.dry_run:
            return True
        if actor is None:
            return False
        try:
            return registry.get(tool).risk in FORCED_DRY_RUN_TIERS
        except KeyError:
            return True

    # A typed turn is answered on the page, never on the device: its own speaker and no
    # voice control (no ``speak`` or ``capture_pin`` reaches the audio service).
    loop = replace(
        deps.loop,
        registry=DryRunRegistry.wrap(registry, force),
        speaker=ListSpeaker(),
        control=None,
    )
    deps.events.publish(
        "turn",
        {"id": turn_id, "profile_id": target.id, "state": "thinking", "transcript": body.text},
        audience="turn",
        subject_profile_id=target.id,
        subject_kind=target.kind,
    )
    utterance = Utterance(
        text=body.text, principal=principal, locale=target.locale, device=body.device
    )
    result = await loop.handle(utterance)
    if result.pending is not None:
        # WEB_UI §5.9: a critical call from the page still exercises the approval flow, so
        # the turn stays open in the background and executes once the banner is approved
        # (the same ``follow_approval`` a voice turn uses; the outcome is published as a
        # ``turn`` event because the page has no speaker).
        _follow_in_background(deps, loop, result, utterance, target.id, target.kind)
    reply = " ".join(t for t in (speech_text(s) for s in result.speech) if t)
    state: Literal["done", "error"] = "error" if result.path == "error" else "done"
    deps.events.publish(
        "turn",
        {"id": turn_id, "profile_id": target.id, "state": state, "reply_delta": reply},
        audience="turn",
        subject_profile_id=target.id,
        subject_kind=target.kind,
    )
    return ChatReply(
        turn_id=turn_id,
        path=result.path,
        reply=reply,
        tool_calls=[
            ToolCallChip(
                tool=c.tool,
                decision=c.decision,
                reason=c.reason,
                risk=c.risk,
                dry_run=c.decision == "allow" and force(c.tool),
            )
            for c in result.tool_calls
        ],
        pending_approval_id=result.pending_approval_id,
        as_profile_id=target.id,
        dry_run=body.dry_run,
    )


__all__ = ["DryRunRegistry", "router"]
