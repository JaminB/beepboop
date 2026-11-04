"""Live events for the web UI: ``GET /api/events`` (docs/WEB_UI.md §8, §6.4).

:class:`EventBus` is the in-process fan-out behind the SSE stream. Every event carries
its *subject* (whose turn, whose approval) and an audience rule; :func:`visible` applies
the §6.4 visibility table before anything is written to a socket, so a member's stream
never carries another adult's ``turn`` events and an ``approval`` event only ever goes to
the profile it is bound to.

The approval nonce (IDENTITY_AND_TRUST §8.2) travels only inside the SSE ``approval``
event. The database stores its hash, so the bus keeps the plaintext in memory until the
approval is decided or expires, to replay it to a session that (re)connects; that is
also the moment the approval's ``exp`` is extended once to 120 s ("the approver's session
opens the approvals stream in time").

:class:`PublishingApprovalService` is the seam that makes this happen without touching the
registry: it *is* the ``ApprovalService`` the registry gets, and publishes on ``create``.
The executor that re-dispatches after a decision (the agent loop, which keeps the voice
turn open until ``exp``) awaits :meth:`EventBus.wait_decision`.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from beepboop.policy.approvals import ApprovalRecord, ApprovalService
from beepboop.policy.types import Principal, ProfileKind

EventName = Literal["turn", "approval", "inbox", "device", "enroll", "health", "update"]
Audience = Literal["subject", "subject_and_admins", "turn", "admins", "all"]

PING_INTERVAL_S = 15.0
REPLAY_BUFFER = 256
QUEUE_MAX = 256


@dataclass(frozen=True, slots=True)
class BusEvent:
    id: int
    event: EventName
    data: Mapping[str, object]
    audience: Audience
    subject_profile_id: str | None = None
    subject_kind: ProfileKind | None = None


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who an SSE stream belongs to."""

    profile_id: str
    kind: ProfileKind


def visible(event: BusEvent, viewer: Viewer) -> Mapping[str, object] | None:
    """The §6.4 filter: the event body this viewer may see, or ``None``."""
    is_admin = viewer.kind == "admin"
    own = event.subject_profile_id == viewer.profile_id
    match event.audience:
        case "all":
            return event.data
        case "admins":
            return event.data if is_admin else None
        case "subject":
            return event.data if own else None
        case "subject_and_admins":
            return event.data if own or is_admin else None
        case "turn":
            if own:
                return event.data
            if not is_admin:
                return None
            if event.subject_kind in ("child", "guest"):
                return event.data
            # Other adults: tool calls without transcripts (WEB_UI §3).
            return {k: v for k, v in event.data.items() if k not in ("transcript", "reply_delta")}


def format_sse(event_id: int, name: str, data: Mapping[str, object]) -> str:
    body = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event_id}\nevent: {name}\ndata: {body}\n\n"


def iso(when: datetime) -> str:
    """ISO 8601 UTC with ``Z`` and millisecond precision (WEB_UI §8)."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def approval_payload(
    record: ApprovalRecord, nonce: str | None, *, state: str | None = None
) -> dict[str, object]:
    out: dict[str, object] = {
        "id": record.id,
        "summary": record.summary,
        "tool": record.tool,
        "exp": iso(record.exp),
        "state": state or record.decision or "pending",
    }
    if nonce is not None:
        out["nonce"] = nonce
    if record.factor is not None:
        out["factor"] = record.factor
    return out


@dataclass
class _Pending:
    record: ApprovalRecord
    nonce: str


@dataclass
class EventBus:
    """In-process pub/sub with a replay buffer and §6.4 filtering."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _next_id: int = 0
    _next_sub: int = 0
    _buffer: deque[BusEvent] = field(default_factory=lambda: deque[BusEvent](maxlen=REPLAY_BUFFER))
    _subscribers: dict[int, tuple[Viewer, asyncio.Queue[BusEvent]]] = field(
        default_factory=dict[int, tuple[Viewer, "asyncio.Queue[BusEvent]"]]
    )
    _pending: dict[str, _Pending] = field(default_factory=dict[str, _Pending])
    _waiters: dict[str, list[asyncio.Future[ApprovalRecord]]] = field(
        default_factory=dict[str, list["asyncio.Future[ApprovalRecord]"]]
    )
    #: Called with the approval id after every decision or extension: the turn loop's
    #: ``notify_approval`` (wired by ``CoreService.start``), so a voice turn waiting on
    #: the approval wakes at once instead of at its next poll.
    approval_listeners: list[Callable[[str], None]] = field(
        default_factory=list[Callable[[str], None]]
    )

    def _notify(self, approval_id: str) -> None:
        for listener in list(self.approval_listeners):
            listener(approval_id)

    # ---------------------------------------------------------------- publish --

    def publish(
        self,
        event: EventName,
        data: Mapping[str, object],
        *,
        audience: Audience,
        subject_profile_id: str | None = None,
        subject_kind: ProfileKind | None = None,
    ) -> BusEvent:
        self._next_id += 1
        ev = BusEvent(self._next_id, event, dict(data), audience, subject_profile_id, subject_kind)
        self._buffer.append(ev)
        for viewer, queue in list(self._subscribers.values()):
            if visible(ev, viewer) is None:
                continue
            if queue.full():
                queue.get_nowait()  # drop the oldest; the client resyncs by polling
            queue.put_nowait(ev)
        return ev

    # ------------------------------------------------------------- approvals --

    def approval_created(self, record: ApprovalRecord, nonce: str) -> None:
        if record.profile_id is None:
            return
        self._pending[record.id] = _Pending(record, nonce)
        self.publish(
            "approval",
            approval_payload(record, nonce),
            audience="subject",
            subject_profile_id=record.profile_id,
        )

    def approval_extended(self, record: ApprovalRecord) -> None:
        pending = self._pending.get(record.id)
        if pending is not None:
            pending.record = record
            self.publish(
                "approval",
                approval_payload(record, pending.nonce),
                audience="subject",
                subject_profile_id=record.profile_id,
            )
        self._notify(record.id)

    def approval_decided(self, record: ApprovalRecord) -> None:
        """Publish the outcome (no nonce) and wake the executor waiting on it."""
        self._pending.pop(record.id, None)
        self.publish(
            "approval",
            approval_payload(record, None),
            audience="subject",
            subject_profile_id=record.profile_id,
        )
        for fut in self._waiters.pop(record.id, []):
            if not fut.done():
                fut.set_result(record)
        self._notify(record.id)

    def pending_for(self, profile_id: str, now: datetime) -> list[tuple[ApprovalRecord, str]]:
        """Live approvals with their nonces, for replay to a (re)connecting stream."""
        out: list[tuple[ApprovalRecord, str]] = []
        for key, p in list(self._pending.items()):
            if p.record.exp <= now:
                del self._pending[key]
                continue
            if p.record.profile_id == profile_id:
                out.append((p.record, p.nonce))
        return out

    def pending_ids(self) -> list[str]:
        return list(self._pending)

    def nonce_for(self, approval_id: str) -> str | None:
        p = self._pending.get(approval_id)
        return None if p is None else p.nonce

    async def wait_decision(self, approval_id: str, wait_s: float) -> ApprovalRecord | None:
        """The executor's seam: the decided record, or ``None`` after ``wait_s`` seconds."""
        fut: asyncio.Future[ApprovalRecord] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(approval_id, []).append(fut)
        try:
            return await asyncio.wait_for(fut, wait_s)
        except TimeoutError:
            return None
        finally:
            waiters = self._waiters.get(approval_id)
            if waiters is not None and fut in waiters:
                waiters.remove(fut)
                if not waiters:
                    del self._waiters[approval_id]

    # ------------------------------------------------------------- subscribe --

    def subscribe(self, viewer: Viewer) -> tuple[int, asyncio.Queue[BusEvent]]:
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._next_sub += 1
        self._subscribers[self._next_sub] = (viewer, queue)
        return self._next_sub, queue

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def replay_after(self, last_id: int, viewer: Viewer) -> list[BusEvent]:
        return [e for e in self._buffer if e.id > last_id and visible(e, viewer) is not None]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


async def sse_stream(
    bus: EventBus,
    viewer: Viewer,
    *,
    last_event_id: int | None,
    initial: Iterable[tuple[str, Mapping[str, object]]] = (),
    ping_interval: float = PING_INTERVAL_S,
) -> AsyncGenerator[str]:
    """The ``text/event-stream`` body for one session (filtered, resumable, pinged)."""
    sub_id, queue = bus.subscribe(viewer)
    try:
        yield "retry: 3000\n\n"
        for name, data in initial:
            yield format_sse(0, name, data)
        if last_event_id is not None:
            for ev in bus.replay_after(last_event_id, viewer):
                body = visible(ev, viewer)
                if body is not None:
                    yield format_sse(ev.id, ev.event, body)
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), ping_interval)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            body = visible(ev, viewer)
            if body is not None:
                yield format_sse(ev.id, ev.event, body)
    finally:
        bus.unsubscribe(sub_id)


@dataclass
class PublishingApprovalService(ApprovalService):
    """``ApprovalService`` that pushes the new approval (with its nonce) to the bus."""

    bus: EventBus = field(default_factory=EventBus)

    async def create(
        self,
        principal: Principal,
        tool: str,
        args: Mapping[str, object],
        target_uuids: Iterable[str],
        summary: str,
        now: datetime,
    ) -> tuple[ApprovalRecord, str]:
        record, nonce = await super().create(principal, tool, args, target_uuids, summary, now)
        self.bus.approval_created(record, nonce)
        return record, nonce


__all__ = [
    "PING_INTERVAL_S",
    "Audience",
    "BusEvent",
    "EventBus",
    "EventName",
    "PublishingApprovalService",
    "Viewer",
    "approval_payload",
    "format_sse",
    "iso",
    "sse_stream",
    "visible",
]
