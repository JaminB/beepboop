"""The turn loop (docs/ARCHITECTURE.md §5, §10, §11; LLM_PROVIDERS §3.6, §7, §8, §10, §12).

``TurnLoop.handle(utterance)`` is the one entry point for every front door that carries a
transcript (voice ``stt.final``, ``beepboop chat``, the web chat). Order:

1. **fast path** (LLM_PROVIDERS §12.2): grammar match → tool arguments → resolve
   (confidence ≥ 0.9, else LLM) → a *pure* pre-check with ``policy.authorize`` (effective
   tier above ``act`` or ``confirm`` → LLM) → ``registry.dispatch`` → speak;
2. otherwise the **LLM loop** through the fallback ladder: stream, chunk sentences to
   ``speak`` as they complete, ``ack`` earcon on the first tool call, every tool call through
   the same ``registry.dispatch`` (≤ 8 per turn), deferred tools appended after
   ``find_tools`` / ``read_guide``, usage and cost summed per turn;
3. **no LLM** (not configured, offline, ladder exhausted): the ``offline`` phrase; the
   fast path keeps working regardless.

Policy exceptions become system phrases (VOICE_PIPELINE §11.6, :mod:`.phrases`). A
``confirm`` pauses the turn with ``approval.prompt`` and (when the profile has a PIN)
``capture_pin``; :meth:`TurnLoop.follow_approval` waits for the phone or the PIN and
re-dispatches with the approval. The loop never decides policy itself: every effect goes
through ``registry.dispatch`` → ``authorize`` (CLAUDE.md hard rule 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import Any, Literal, cast

import jsonschema

from beepboop.agent import phrases
from beepboop.agent.chunking import SentenceChunker, clean_for_speech
from beepboop.agent.context import PromptInputs, assemble_request, prefix_hash, wrap_untrusted
from beepboop.agent.fastpath import FastPath, FastPathMatch
from beepboop.agent.types import (
    MAX_TOOL_CALLS_PER_TURN,
    NOT_ME_WINDOW_S,
    ConversationStore,
    PendingApproval,
    PinStore,
    PreviousTurn,
    ProfileDirectory,
    Speaker,
    Speech,
    ToolCallSummary,
    TurnLog,
    TurnResult,
    TurnStart,
    Utterance,
    VoiceControl,
)
from beepboop.agent.usage import TurnUsage
from beepboop.log import get_logger
from beepboop.policy.approvals import ApprovalRecord, TooManyPendingError
from beepboop.policy.authorize import authorize
from beepboop.policy.errors import NeedsConfirmation, PolicyDenied
from beepboop.policy.pin import PinOutcome, record_outcome
from beepboop.policy.types import DenyReason, Principal, ProfileKind
from beepboop.providers.llm.fallback import Ladder, LadderExhausted, LadderSetupError, Rung
from beepboop.providers.llm.protocol import (
    Done,
    LLMProvider,
    Message,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
    ToolUsePart,
    Usage,
)
from beepboop.registry.registry import (
    ArgsInvalidError,
    DispatchResult,
    RegisteredTool,
    Registry,
    ToolNotFoundError,
)
from beepboop.registry.resolver import EntityIndex, TargetQuery, resolve

#: Re-exported for callers of the v0.1 skeleton API.
deny_phrase = phrases.deny_phrase
DENY_PHRASES = phrases.DENY_PHRASES

REFUSAL = "I can't help with that one."
_FAST_TIERS = frozenset({"read", "act"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _span(
    result: TurnResult,
    tool: str,
    started: float,
    risk: str,
    decision: str,
    factor: str | None = None,
) -> None:
    """OPERATIONS §11: one ``tool.<name>`` span per call with ``risk``, ``decision`` and
    ``factor`` (a second call to the same tool gets a ``#2`` suffix)."""
    key = f"tool.{tool}"
    n = 2
    while key in result.stages_ms:
        key = f"tool.{tool}#{n}"
        n += 1
    span: dict[str, object] = {
        "ms": round((time.monotonic() - started) * 1000, 1),
        "risk": risk,
        "decision": decision,
    }
    if factor:
        span["factor"] = factor
    result.stages_ms[key] = span


def _pin_waiter_map() -> dict[str, asyncio.Future[PinOutcome]]:
    return {}


class NoLLM:
    """Stands in when no provider is configured: every turn is fast-path or offline.

    ``reason="unconfigured"`` (no credentials yet, ARCHITECTURE §10) speaks the setup
    sentence instead of the ``offline`` phrase.
    """

    id = "none"

    def __init__(self, reason: Literal["offline", "unconfigured"] = "offline") -> None:
        self.reason = reason


class StaticDirectory:
    """:class:`ProfileDirectory` from plain dicts (tests, ``chat``)."""

    def __init__(
        self,
        kinds: dict[str, ProfileKind] | None = None,
        names: dict[str, str] | None = None,
        admin: str = "an adult",
    ) -> None:
        self.kinds = kinds or {}
        self.names = names or {}
        self.admin = admin

    def kind(self, profile_id: str) -> ProfileKind:
        if profile_id == "guest":
            return "guest"
        return self.kinds.get(profile_id, "member")

    def name(self, profile_id: str) -> str:
        if profile_id == "guest":
            return "Guest"
        return self.names.get(profile_id, profile_id)

    def admin_name(self) -> str:
        return self.admin


class NullTurnLog:
    """:class:`TurnLog` that keeps nothing (tests that do not look at rows)."""

    async def start(self, turn: TurnStart) -> None:
        return None

    async def finish(self, turn: TurnStart, transcript: str, result: TurnResult) -> None:
        return None

    async def previous(self, device: str, before_turn_id: str) -> PreviousTurn | None:
        return None

    async def not_me(self, turn_id: str, *, corrector: Principal, disputed: bool) -> None:
        return None

    async def forget(self, turn_id: str) -> bool:
        return False

    async def flag(self, turn_id: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _FastPlan:
    match: FastPathMatch
    tool: RegisteredTool | None
    args: dict[str, Any]


@dataclass(slots=True)
class _Outcome:
    kind: Literal["allow", "deny", "confirm", "error"]
    out: DispatchResult | None = None
    reason: DenyReason | None = None
    pending: PendingApproval | None = None
    code: str | None = None


@dataclass
class TurnLoop:
    registry: Registry
    speaker: Speaker
    conversation: ConversationStore
    prompt_inputs: Callable[[Utterance], PromptInputs | Awaitable[PromptInputs]]
    provider: LLMProvider | Ladder | NoLLM = field(default_factory=NoLLM)
    fastpath: FastPath | None = None
    directory: ProfileDirectory = field(default_factory=StaticDirectory)
    clock: Callable[[], datetime] = _utcnow
    tz: tzinfo = UTC
    entity_index: Callable[[], EntityIndex | None] = lambda: None
    turn_log: TurnLog = field(default_factory=NullTurnLog)
    control: VoiceControl | None = None
    pins: PinStore | None = None
    approval_poll_s: float = 0.25
    #: ``beepboop chat --dry-run``: handlers run with ``ctx.dry_run`` and effects are recorded.
    dry_run: bool = False
    _device_owner: dict[str, str] = field(default_factory=dict[str, str])
    _approval_events: dict[str, asyncio.Event] = field(default_factory=dict[str, asyncio.Event])
    _pin_waiters: dict[str, asyncio.Future[PinOutcome]] = field(default_factory=_pin_waiter_map)

    # ------------------------------------------------------------------ public --

    @property
    def ladder(self) -> Ladder | None:
        if isinstance(self.provider, NoLLM):
            return None
        if isinstance(self.provider, Ladder):
            return self.provider
        self.provider = Ladder.single(self.provider)
        return self.provider

    async def warm(self) -> None:
        """On ``wake``: pre-warm the first rung (§3.7). Fire-and-forget; never raises."""
        ladder = self.ladder
        if ladder is None:
            return
        try:
            await ladder.warm()
        except Exception as exc:
            get_logger(__name__).debug("llm.warm_failed", error=type(exc).__name__)

    async def handle(self, utterance: Utterance) -> TurnResult:
        """One utterance → one ``TurnResult`` (spoken along the way, recorded at the end)."""
        p = utterance.principal
        result = TurnResult(turn_id=p.turn_id, path="llm")
        result.stages_ms.update(utterance.stages_ms)
        start = TurnStart(
            p.turn_id,
            p,
            utterance.locale,
            utterance.device,
            utterance.stt_provider,
            utterance.tts_provider,
            utterance.id_stage,
            self.clock(),
        )
        log = get_logger(__name__).bind(turn_id=p.turn_id, profile_id=p.profile_id)
        refresh = getattr(self.directory, "refresh", None)
        if refresh is not None:
            await cast(Callable[[], Any], refresh)()
        try:
            await self.turn_log.start(start)
        except Exception as exc:
            log.warning("turn.log_start_failed", error=type(exc).__name__)
        try:
            await self._run(utterance, result)
        except asyncio.CancelledError:
            result.cancelled = True
            result.error = result.error or "cancelled"
            raise
        except Exception as exc:
            log.exception("turn.failed", error=type(exc).__name__)
            result.path = "error"
            result.error = type(exc).__name__
            await self._say(result, Speech(clip="offline"), last=True)
        finally:
            await self._finish(start, utterance, result)
        return result

    async def follow_approval(self, result: TurnResult, utterance: Utterance) -> None:
        """Keep the turn open until the pending approval is decided, executed or expired
        (IDENTITY_AND_TRUST §8.3): ``approval.done`` / ``approval.denied`` /
        ``approval.waiting`` (once) / ``approval.expired``; a spoken PIN decides it with
        ``factor='pin'``. Cancelling the turn leaves the approval pending until ``exp``."""
        pending = result.pending
        if pending is None:
            return
        p = utterance.principal
        log = get_logger(__name__).bind(turn_id=p.turn_id, approval_id=pending.approval_id)
        store = self.registry.approvals.store
        event = self._approval_events.setdefault(pending.approval_id, asyncio.Event())
        pin_fut = self._pin_waiters.get(pending.approval_id)
        said_waiting = False
        try:
            while True:
                record = await store.get(pending.approval_id)
                now = self.clock()
                if record is None:
                    return
                if record.decision == "approved" and record.executed_at is None:
                    await self._execute_approved(utterance, result, pending, record)
                    return
                if record.decision == "denied":
                    await self._say(result, phrases.phrase("approval.denied"), last=True)
                    return
                if record.decision == "expired" or record.exp <= now:
                    await self._say(result, phrases.phrase("approval.expired"), last=True)
                    return
                if record.executed_at is not None:
                    return
                if record.extended and not said_waiting:
                    said_waiting = True
                    await self._say(result, phrases.phrase("approval.waiting"), last=False)
                if pin_fut is not None and pin_fut.done():
                    outcome = pin_fut.result()
                    pin_fut = None
                    if await self._apply_pin(p, pending, outcome, result):
                        continue  # decided as approved; the next pass executes it
                remaining = (record.exp - now).total_seconds()
                waits: list[asyncio.Future[Any]] = [asyncio.ensure_future(event.wait())]
                if pin_fut is not None:
                    waits.append(pin_fut)
                done, _ = await asyncio.wait(
                    waits,
                    timeout=max(0.0, min(self.approval_poll_s, remaining)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                del done
                waits[0].cancel()
                event.clear()
        except asyncio.CancelledError:
            log.info("approval.turn_closed")
            if pin_fut is not None and not pin_fut.done() and self.control is not None:
                with contextlib.suppress(Exception):
                    await self.control.cancel(p.turn_id, "superseded")
            raise
        finally:
            self._approval_events.pop(pending.approval_id, None)
            self._pin_waiters.pop(pending.approval_id, None)

    def notify_approval(self, approval_id: str) -> None:
        """The approvals API calls this after a decide or an extension: wakes the waiter
        at once instead of at the next poll."""
        event = self._approval_events.get(approval_id)
        if event is not None:
            event.set()

    def deliver_pin_result(self, approval_id: str, outcome: PinOutcome) -> bool:
        """Route a ``pin.result`` to the open turn waiting for it; ``False`` if none is."""
        fut = self._pin_waiters.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(outcome)
        return True

    # ------------------------------------------------------------- the turn --

    async def _run(self, utterance: Utterance, result: TurnResult) -> None:
        t0 = time.monotonic()
        plan = await self._fast_plan(utterance)
        result.stages_ms["fastpath"] = round((time.monotonic() - t0) * 1000, 1)
        if plan is not None:
            result.path = "fast"
            if plan.tool is None:
                await self._core_action(plan.match, utterance, result)
                return
            await self._fast_dispatch(utterance, plan, result)
            return
        if isinstance(self.provider, NoLLM):
            result.path = "offline"
            if self.provider.reason == "unconfigured":
                await self._say(result, Speech(text=phrases.SETUP_NEEDED), last=True)
            else:
                await self._say(result, phrases.phrase("offline"), last=True)
            return
        await self._llm_turn(utterance, result, t0)

    async def _finish(self, start: TurnStart, utterance: Utterance, result: TurnResult) -> None:
        if result.pending is not None:
            result.pending_approval_id = result.pending.approval_id
        try:
            await self.turn_log.finish(start, utterance.text, result)
        except Exception as exc:
            get_logger(__name__).warning(
                "turn.log_finish_failed", turn_id=result.turn_id, error=type(exc).__name__
            )

    # --------------------------------------------------------------- fast path --

    async def _fast_plan(self, utterance: Utterance) -> _FastPlan | None:
        """Steps 1-4 of §12.2 without side effects; ``None`` hands the turn to the LLM."""
        if self.fastpath is None:
            return None
        match = self.fastpath.match(utterance.text)
        if match is None:
            return None
        if match.tool is None:
            return _FastPlan(match, None, {})
        try:
            tool = self.registry.get(match.tool)
        except ToolNotFoundError:
            return None
        args = match.build_args(self.clock().astimezone(self.tz))
        if args is None:
            return None
        try:
            jsonschema.validate(args, tool.spec.args)
        except jsonschema.ValidationError:
            return None
        p = replace(utterance.principal, tainted=False)  # §3.2: the user's own words
        target = args.get("target")
        if isinstance(target, dict):
            index = self.entity_index()
            if index is None:
                return None
            t = cast(dict[str, Any], target)
            query = TargetQuery(
                name=cast(str | None, t.get("name")),
                area=cast(str | None, t.get("area")),
                domain=cast(str | None, t.get("domain")),
            )
            if not resolve(p, query, index).fastpath_ok:
                return None
        targets = await self.registry.targets(p, tool, args)
        decision = authorize(self.registry.policy_state(), p, tool, args, targets, self.clock())
        if decision.effective_risk not in _FAST_TIERS or decision.outcome == "confirm":
            return None
        return _FastPlan(match, tool, args)

    async def _fast_dispatch(
        self, utterance: Utterance, plan: _FastPlan, result: TurnResult
    ) -> None:
        assert plan.tool is not None  # noqa: S101 - checked by the caller
        p = replace(utterance.principal, tainted=False)
        outcome = await self._dispatch(p, utterance, plan.tool.name, plan.args, result)
        if outcome.kind == "deny":
            await self._say(result, self._deny_speech(p, plan.tool.name, outcome.reason), last=True)
        elif (
            outcome.kind == "confirm"
        ):  # pragma: no cover - the pre-check routes confirm to the LLM
            await self._speak_confirm(p, result, outcome)
        elif outcome.code == "too_many_pending":
            await self._say(result, Speech(text=phrases.TOO_MANY_PENDING), last=True)
        elif outcome.out is None:
            await self._say(result, Speech(text=phrases.TOOL_FAILED), last=True)
        else:
            await self._say(result, _result_speech(outcome.out, default_clip="done"), last=True)

    async def _core_action(
        self, match: FastPathMatch, utterance: Utterance, result: TurnResult
    ) -> None:
        p = utterance.principal
        if match.intent == "time_date":
            now = self.clock().astimezone(self.tz)
            if "date" in match.template or "day" in match.template:
                text = f"It's {now.strftime('%A')}, {now.strftime('%B')} {now.day}."
            else:
                hour = now.hour % 12 or 12
                text = f"It's {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}."
            await self._say(result, Speech(text=text), last=True)
            return
        if match.intent == "stop":
            await self._say(result, phrases.phrase("cancel"), last=True)
            return
        prev = await self.turn_log.previous(utterance.device, p.turn_id)
        if match.intent == "not_me":
            age = None if prev is None else (self.clock() - prev.created_at).total_seconds()
            if prev is None or age is None or age > NOT_ME_WINDOW_S:
                await self._say(
                    result, Speech(text="I don't have a recent request to correct."), last=True
                )
                return
            disputed = (
                p.profile_id != prev.profile_id and self.directory.kind(p.profile_id) != "admin"
            )
            await self.turn_log.not_me(prev.turn_id, corrector=p, disputed=disputed)
            await self._say(result, phrases.phrase("not_me"), last=True)
            return
        if match.intent == "forget_last":
            if (
                prev is not None
                and prev.profile_id == p.profile_id
                and await self.turn_log.forget(prev.turn_id)
            ):
                await self.conversation.clear(p.profile_id, utterance.device)
                await self._say(result, phrases.phrase("forget"), last=True)
                return
            await self._say(result, Speech(text="There's nothing of yours to forget."), last=True)
            return
        await self._say(result, Speech(clip="done"), last=True)  # pragma: no cover - grammar drift

    # ---------------------------------------------------------------- dispatch --

    async def _dispatch(
        self,
        principal: Principal,
        utterance: Utterance,
        tool: str,
        args: dict[str, Any],
        result: TurnResult,
    ) -> _Outcome:
        """``registry.dispatch`` and the bookkeeping every caller needs."""
        t_tool = time.monotonic()
        try:
            out = await self.registry.dispatch(principal, tool, args, dry_run=self.dry_run)
        except PolicyDenied as exc:
            result.tool_calls.append(ToolCallSummary(tool, "deny", exc.reason, exc.risk))
            _span(result, tool, t_tool, exc.risk, "deny")
            return _Outcome("deny", reason=exc.reason)
        except NeedsConfirmation as exc:
            risk = self._risk_of(tool)
            result.tool_calls.append(ToolCallSummary(tool, "confirm", None, risk))
            rec = exc.approval
            pending = PendingApproval(rec.id, tool, dict(args), rec.summary, exc.factors, rec.exp)
            result.pending = pending
            result.pending_approval_id = rec.id
            _span(result, tool, t_tool, risk, "confirm", ",".join(exc.factors))
            return _Outcome("confirm", pending=pending)
        except TooManyPendingError:
            result.tool_calls.append(ToolCallSummary(tool, "confirm", None, self._risk_of(tool)))
            return _Outcome("error", code="too_many_pending")
        except (ArgsInvalidError, ToolNotFoundError) as exc:
            result.tool_calls.append(ToolCallSummary(tool, "error", type(exc).__name__))
            return _Outcome("error")
        status = "ok" if "error" not in out.result.structured else "error"
        result.tool_calls.append(
            ToolCallSummary(
                tool, "allow", None, out.decision.effective_risk, out.duration_ms, status
            )
        )
        _span(result, tool, t_tool, out.decision.effective_risk, "allow")
        if out.tainted and not result.tainted:
            result.tainted = True
            await self.conversation.taint(principal.profile_id, utterance.device)
        return _Outcome("allow", out=out)

    def _risk_of(self, tool: str) -> str:
        try:
            return self.registry.get(tool).risk
        except ToolNotFoundError:
            return "read"

    def _deny_speech(self, p: Principal, tool: str, reason: DenyReason | None) -> Speech:
        spoken_name: str | None = None
        registered = self._tool_or_none(tool)
        if registered is not None:
            manifest = self.registry.manifest(registered.module_id)
            if manifest is not None:
                spoken_name = manifest.display_spoken_name if manifest.spoken_name else None
        return phrases.deny_speech(
            reason or "no_grant",
            self.directory.kind(p.profile_id),
            tool=tool,
            name=self.directory.name(p.profile_id),
            admin=self.directory.admin_name(),
            via=p.via,
            spoken_name=spoken_name,
        )

    async def _speak_confirm(self, p: Principal, result: TurnResult, outcome: _Outcome) -> None:
        """``approval.prompt`` / ``approval.prompt_pin`` / ``personal.unsure`` /
        ``approval.no_login`` (+ PIN), then ``capture_pin`` when the profile has a PIN."""
        pending = outcome.pending
        assert pending is not None  # noqa: S101 - set with kind="confirm"
        kind = self.directory.kind(p.profile_id)
        name = self.directory.name(p.profile_id)
        can_pin = "pin" in pending.factors and p.via == "voice" and not p.tainted
        pin_hash = await self._pin_hash(p.profile_id) if can_pin else None
        has_web = "web" in pending.factors
        if kind == "guest":  # §11.6 rule 1 (policy never confirms for guest; belt and braces)
            speech = phrases.phrase("unknown_speaker")
        elif not has_web:
            speech = phrases.phrase("approval.no_login", admin=self.directory.admin_name())
        elif self._risk_of(pending.tool) == "personal" and p.via == "voice":
            registered = self._tool_or_none(pending.tool)
            manifest = None if registered is None else self.registry.manifest(registered.module_id)
            text = phrases.render(
                "personal.unsure",
                name=name,
                thing=phrases.thing_for(
                    pending.tool, None if manifest is None else manifest.spoken_name
                ),
            )
            if pin_hash is None:
                text = text.removesuffix(" Approve on your phone, or say your PIN.") + (
                    " Approve on your phone."
                )
            speech = Speech(text=text, clip="personal.unsure")
        elif pin_hash is not None:
            speech = phrases.phrase("approval.prompt_pin", summary=pending.summary)
        else:
            speech = phrases.phrase("approval.prompt", summary=pending.summary)
        await self._say(result, speech, last=True)
        if pin_hash is not None and self.control is not None and kind != "guest":
            fut: asyncio.Future[PinOutcome] = asyncio.get_running_loop().create_future()
            self._pin_waiters[pending.approval_id] = fut
            await self.control.capture_pin(
                p.turn_id, pending.approval_id, pin_hash, phrases.render("pin.prompt")
            )

    async def _pin_hash(self, profile_id: str) -> str | None:
        if self.pins is None or self.control is None:
            return None
        pin_hash, state = await self.pins.load(profile_id)
        if pin_hash is None or not state.enabled or state.is_locked(self.clock()):
            return None
        return pin_hash

    async def _apply_pin(
        self, p: Principal, pending: PendingApproval, outcome: PinOutcome, result: TurnResult
    ) -> bool:
        """§8.4: ``ok`` decides the approval with ``factor='pin'``; a mismatch counts toward
        lockout and the phone path stays open. Returns whether the approval is now approved."""
        now = self.clock()
        if self.pins is not None:
            _hash, state = await self.pins.load(p.profile_id)
            transition = record_outcome(state, outcome, now)
            await self.pins.save(p.profile_id, transition.state)
            if transition.event in ("locked", "disabled"):
                await self.turn_log.flag(p.turn_id)  # §11: a PIN lockout flags the turn
                await self._say(result, phrases.phrase("pin.locked"), last=False)
                return False
        if outcome == "ok":
            decided = await self.registry.approvals.decide(
                pending.approval_id, p.profile_id, "approved", "pin", now
            )
            return isinstance(decided, ApprovalRecord)
        if outcome == "mismatch":
            await self._say(result, phrases.phrase("pin.mismatch"), last=False)
        return False

    async def _execute_approved(
        self,
        utterance: Utterance,
        result: TurnResult,
        pending: PendingApproval,
        record: ApprovalRecord,
    ) -> None:
        p = replace(utterance.principal, tainted=result.tainted)
        t_tool = time.monotonic()
        try:
            out = await self.registry.dispatch(
                p, pending.tool, pending.args, approval=record, dry_run=self.dry_run
            )
        except PolicyDenied as exc:
            result.tool_calls.append(ToolCallSummary(pending.tool, "deny", exc.reason, exc.risk))
            _span(result, pending.tool, t_tool, exc.risk, "deny", record.factor)
            await self._say(result, phrases.phrase("approval.expired"), last=True)
            return
        except NeedsConfirmation:  # pragma: no cover - an approved record satisfies the factor
            await self._say(result, phrases.phrase("approval.expired"), last=True)
            return
        result.tool_calls.append(
            ToolCallSummary(
                pending.tool, "allow", None, out.decision.effective_risk, out.duration_ms, "ok"
            )
        )
        _span(result, pending.tool, t_tool, out.decision.effective_risk, "allow", record.factor)
        past = out.result.speak.strip().rstrip(".") if out.result.speak else ""
        past = past or pending.summary
        summary_past = past[:1].upper() + past[1:]
        await self._say(
            result, phrases.phrase("approval.done", summary_past=summary_past), last=True
        )

    # ----------------------------------------------------------------- llm loop --

    async def _llm_turn(self, utterance: Utterance, result: TurnResult, t0: float) -> None:
        ladder = self.ladder
        assert ladder is not None  # noqa: S101 - NoLLM handled by the caller
        p = utterance.principal
        device = utterance.device
        continuing = p.profile_id != "guest" and self._device_owner.get(device) == p.profile_id
        if not continuing:
            await self.conversation.clear(p.profile_id, device)
        self._device_owner[device] = p.profile_id
        history_raw, tainted = await self.conversation.load(p.profile_id, device)
        history = _parse_history(history_raw) if continuing else []
        result.tainted = tainted if continuing else False

        t_ctx = time.monotonic()
        raw_inputs = self.prompt_inputs(utterance)
        base_inputs = await raw_inputs if inspect.isawaitable(raw_inputs) else raw_inputs
        inputs = replace(
            base_inputs,
            profile_id=p.profile_id,
            speaker_name=self.directory.name(p.profile_id),
            speaker_kind=self.directory.kind(p.profile_id),
            continuing=bool(history),
        )
        result.prefix_hash = prefix_hash(inputs)
        loaded = {u.name for m in history for u in m.content if isinstance(u, ToolUsePart)}
        turn_msgs: list[Message] = [Message(role="user", content=[TextPart(text=utterance.text)])]
        result.stages_ms["ctx"] = round((time.monotonic() - t_ctx) * 1000, 1)

        usage = TurnUsage()
        rung_box: list[Rung] = []
        calls = 0
        ack_sent = False
        spoke_any = False
        try:
            while True:
                extra = [t for n in sorted(loaded) if (t := self._tool_or_none(n)) is not None]
                step_inputs = replace(inputs, extra_tools=extra)
                result.tools_offered = max(
                    result.tools_offered, len(step_inputs.core_tools) + len(extra)
                )
                req = assemble_request(
                    step_inputs, history, "", turn_id=p.turn_id, turn_messages=turn_msgs
                )
                chunker = SentenceChunker()
                text_seen = False
                tool_uses: list[ToolUsePart] = []
                names: dict[str, str] = {}
                stop: str = "end_turn"
                t_req = time.monotonic()
                async for event in ladder.stream(req, on_rung=rung_box.append):
                    if isinstance(event, TextDelta):
                        if not text_seen and "llm_ttft" not in result.stages_ms:
                            result.stages_ms["llm_ttft"] = round(
                                (time.monotonic() - t_req) * 1000, 1
                            )
                        text_seen = True
                        for sentence in chunker.feed(event.text):
                            spoke_any = True
                            await self._say_sentence(result, sentence, t_req, last=False)
                    elif isinstance(event, ToolCallStart):
                        names[event.id] = event.name
                        if "llm_ttft" not in result.stages_ms:
                            result.stages_ms["llm_ttft"] = round(
                                (time.monotonic() - t_req) * 1000, 1
                            )
                        if not ack_sent:
                            ack_sent = True
                            await self.speaker.earcon(p.turn_id, "ack")
                    elif isinstance(event, ToolCallEnd):
                        tool_uses.append(
                            ToolUsePart(
                                id=event.id, name=names.get(event.id, ""), input=event.input
                            )
                        )
                    elif isinstance(event, Usage):
                        rung = rung_box[-1] if rung_box else None
                        usage.add(
                            event,
                            rung.model_id if rung else None,
                            ttl=rung.cache_ttl if rung else "5m",
                        )
                    elif isinstance(event, Done):
                        stop = event.stop_reason
                        result.stop_reason = event.stop_reason
                tail = chunker.finish()
                assistant: list[TextPart | ToolUsePart | ToolResultPart] = []
                if chunker.text:
                    assistant.append(TextPart(text=chunker.text))
                assistant.extend(tool_uses)
                if assistant:
                    turn_msgs.append(Message(role="assistant", content=assistant))

                if stop == "error":  # lost the connection mid-stream (§3.6)
                    text = f"{tail} {phrases.LOST_CONNECTION}" if tail else phrases.LOST_CONNECTION
                    await self._say_sentence(result, text, t_req, last=True)
                    break
                if not tool_uses or stop != "tool_use":
                    if tail:
                        await self._say_sentence(result, tail, t_req, last=True)
                    elif stop == "refusal":
                        await self._say(result, Speech(text=REFUSAL), last=True)
                    elif not spoke_any:
                        await self._say(result, Speech(clip="done"), last=True)
                    else:  # every sentence already left with last=False: close the reply
                        await self._say(result, Speech(clip="done"), last=True)
                    break
                if tail:
                    spoke_any = True
                    await self._say_sentence(result, tail, t_req, last=False)

                results: list[ToolResultPart] = []
                ended: Speech | None = None
                for use in tool_uses:
                    if ended is not None or calls >= MAX_TOOL_CALLS_PER_TURN:
                        results.append(_not_run(use.id))
                        continue
                    calls += 1
                    principal = replace(p, tainted=result.tainted)
                    outcome = await self._dispatch(
                        principal, utterance, use.name, dict(use.input), result
                    )
                    if outcome.kind == "allow" and outcome.out is not None:
                        results.append(self._tool_result(use, outcome.out))
                        loaded.update(_loaded_tools(use.name, outcome.out))
                    elif outcome.kind == "deny":
                        results.append(
                            ToolResultPart(
                                tool_use_id=use.id,
                                content=json.dumps({"error": "denied", "reason": outcome.reason}),
                                is_error=True,
                            )
                        )
                        ended = self._deny_speech(principal, use.name, outcome.reason)
                    elif outcome.kind == "confirm":
                        results.append(
                            ToolResultPart(
                                tool_use_id=use.id,
                                content=json.dumps({"status": "pending_approval"}),
                                is_error=True,
                            )
                        )
                        ended = Speech()  # the confirm phrase is spoken below
                        await self._speak_confirm(principal, result, outcome)
                    elif outcome.code == "too_many_pending":
                        results.append(_error_result(use.id, "too_many_pending"))
                        ended = Speech(text=phrases.TOO_MANY_PENDING)
                    else:
                        results.append(_error_result(use.id, "invalid_call"))
                turn_msgs.append(Message(role="user", content=list(results)))
                if ended is not None:
                    if ended.text or ended.clip:
                        await self._say(result, ended, last=True)
                    break
                if calls >= MAX_TOOL_CALLS_PER_TURN:
                    await self._say(result, Speech(clip="done"), last=True)
                    break
        except LadderSetupError:
            result.path = "error"
            result.error = "llm_setup"
            await self._say(result, Speech(text=phrases.SETUP_NEEDED), last=True)
        except LadderExhausted as exc:
            result.path = "offline"
            result.error = type(exc).__name__
            await self._say(result, phrases.phrase("offline"), last=True)
        finally:
            if rung_box:
                result.rung = rung_box[-1].name
                result.llm_provider = rung_box[-1].label
                result.stages_ms["rung"] = rung_box[-1].name
            if result.prefix_hash:
                result.stages_ms["prefix_hash"] = result.prefix_hash[:16]
            result.tokens_in = usage.input_tokens + usage.cache_write_tokens
            result.tokens_out = usage.output_tokens
            result.tokens_cached = usage.cache_read_tokens
            result.cost_micro_usd = usage.cost_micro_usd
            if usage.estimate:
                result.stages_ms["cost_estimate"] = True
            # DATA_AND_PRIVACY §8: nothing about a guest outlives the turn.
            if p.profile_id != "guest" and (len(turn_msgs) > 1 or result.path == "llm"):
                await self.conversation.save(
                    p.profile_id,
                    device,
                    [m.model_dump() for m in [*history, *turn_msgs]],
                    result.tainted,
                )

    def _tool_or_none(self, name: str) -> RegisteredTool | None:
        try:
            return self.registry.get(name)
        except ToolNotFoundError:
            return None

    def _tool_result(self, use: ToolUsePart, out: DispatchResult) -> ToolResultPart:
        content = out.result.text or json.dumps(
            out.result.structured, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        untrusted = out.tool.spec.untrusted_output
        if untrusted:
            content = wrap_untrusted(out.tool.name, content)
        return ToolResultPart(
            tool_use_id=use.id,
            content=content,
            is_error="error" in out.result.structured,
            untrusted=untrusted,
        )

    # ------------------------------------------------------------------- speech --

    async def _say_sentence(
        self, result: TurnResult, text: str, t_req: float, *, last: bool
    ) -> None:
        """``llm_first_sentence`` is request sent → first sentence boundary (OPERATIONS §11)."""
        if "llm_first_sentence" not in result.stages_ms:
            result.stages_ms["llm_first_sentence"] = round((time.monotonic() - t_req) * 1000, 1)
        clean = clean_for_speech(text)
        if not clean:
            if last:
                await self._say(result, Speech(clip="done"), last=True)
            return
        await self._say(
            result, Speech(text=clean, listen_after=last and clean.endswith("?")), last=last
        )

    async def _say(self, result: TurnResult, speech: Speech, *, last: bool) -> None:
        result.speech.append(speech)
        await self.speaker.speak(result.turn_id, speech, last=last)


# --------------------------------------------------------------------- helpers --


def _parse_history(raw: list[dict[str, object]]) -> list[Message]:
    out: list[Message] = []
    for m in raw:
        try:
            out.append(Message.model_validate(m))
        except ValueError:
            return []  # a corrupt window starts fresh rather than half-loaded
    return out


def _result_speech(out: DispatchResult, *, default_clip: str) -> Speech:
    if out.result.speak:
        return Speech(text=clean_for_speech(out.result.speak))
    error = out.result.structured.get("error")
    code = out.result.structured.get("code")
    if isinstance(error, str) and error and code not in ("other", "timeout", None):
        return Speech(text=error)
    if error:
        return Speech(text=phrases.TOOL_FAILED)
    return Speech(clip=default_clip)


def _loaded_tools(tool: str, out: DispatchResult) -> set[str]:
    """Deferred tools ``find_tools`` / ``read_guide`` loaded for the next step (MCP §6, §7)."""
    if tool not in ("find_tools", "read_guide"):
        return set()
    raw = out.result.structured.get("tools")
    names: set[str] = set()
    if isinstance(raw, list):
        for item in cast(list[object], raw):
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                name = cast(dict[str, object], item).get("name")
                if isinstance(name, str):
                    names.add(name)
    return names


def _not_run(tool_use_id: str) -> ToolResultPart:
    return _error_result(tool_use_id, "not_run")


def _error_result(tool_use_id: str, code: str) -> ToolResultPart:
    return ToolResultPart(
        tool_use_id=tool_use_id, content=json.dumps({"error": code}), is_error=True
    )


class MemoryConversation:
    """In-memory :class:`ConversationStore` for tests and ``beepboop chat``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[list[dict[str, object]], bool]] = {}

    async def load(self, profile_id: str, device: str) -> tuple[list[dict[str, object]], bool]:
        return self.rows.get((profile_id, device), ([], False))

    async def save(
        self, profile_id: str, device: str, messages: list[dict[str, object]], tainted: bool
    ) -> None:
        self.rows[(profile_id, device)] = (messages, tainted)

    async def taint(self, profile_id: str, device: str) -> None:
        msgs, _ = self.rows.get((profile_id, device), ([], False))
        self.rows[(profile_id, device)] = (msgs, True)

    async def clear(self, profile_id: str, device: str) -> None:
        self.rows.pop((profile_id, device), None)


class ListSpeaker:
    """Collects speech instead of sending ``speak`` events."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, Speech, bool]] = []
        self.earcons: list[tuple[str, str]] = []

    async def speak(self, turn_id: str, speech: Speech, *, last: bool) -> None:
        self.spoken.append((turn_id, speech, last))

    async def earcon(self, turn_id: str, name: str) -> None:
        self.earcons.append((turn_id, name))


__all__ = [
    "DENY_PHRASES",
    "REFUSAL",
    "ListSpeaker",
    "MemoryConversation",
    "NoLLM",
    "NullTurnLog",
    "StaticDirectory",
    "TurnLoop",
    "deny_phrase",
]
