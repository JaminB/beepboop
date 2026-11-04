"""The approval record and its two guarded transitions (IDENTITY_AND_TRUST §8.2).

``pending ──(a factor decides)──▶ approved | denied | expired ──(executor)──▶ executed``

Storage is behind :class:`ApprovalStore` so the state machine is testable in memory; the
database implementation is ``beepboop.db.repo.approvals``. Both transitions are
single guarded updates: exactly one row changes or the caller loses.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from beepboop.policy.digest import args_digest
from beepboop.policy.types import Approval, Factor, Principal

ApprovalDecision = Literal["approved", "denied", "expired"]
DecideError = Literal["already_decided", "expired", "not_yours", "bad_nonce", "not_found"]

#: ``exp = created + 60 s``, extended once to 120 s (§8.2).
APPROVAL_TTL = timedelta(seconds=60)
APPROVAL_EXTENDED_TTL = timedelta(seconds=120)
MAX_PENDING_PER_PROFILE = 3
NONCE_BYTES = 16  # 128-bit


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One ``approvals`` row. The ``id`` is public (URLs); the nonce never is."""

    id: str
    nonce_hash: str
    profile_id: str | None
    turn_id: str | None
    tool: str
    args_digest: str
    summary: str
    created_at: datetime
    exp: datetime
    decision: ApprovalDecision | None = None
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    factor: Factor | None = None
    extended: bool = False

    @property
    def pending(self) -> bool:
        return self.decision is None

    def is_live(self, now: datetime) -> bool:
        return self.pending and self.exp > now

    def to_policy(self) -> Approval:
        """The slice ``authorize()`` re-checks on re-dispatch."""
        return Approval(
            id=self.id,
            profile_id=self.profile_id,
            tool=self.tool,
            args_digest=self.args_digest,
            exp=self.exp,
            decision=self.decision,
            executed_at=self.executed_at,
        )


class TooManyPendingError(RuntimeError):
    """More than :data:`MAX_PENDING_PER_PROFILE` live approvals for one profile."""


class ApprovalStore(Protocol):
    """Persistence seam. Every method is one durable write or one read."""

    async def insert(self, record: ApprovalRecord) -> None: ...
    async def get(self, approval_id: str) -> ApprovalRecord | None: ...
    async def pending_for(self, profile_id: str, now: datetime) -> list[ApprovalRecord]: ...
    async def decide(
        self,
        approval_id: str,
        profile_id: str,
        decision: ApprovalDecision,
        factor: Factor | None,
        now: datetime,
    ) -> bool:
        """``UPDATE … WHERE id=? AND profile_id=? AND decision IS NULL AND exp > now``."""
        ...

    async def mark_executed(self, approval_id: str, now: datetime) -> bool:
        """``UPDATE … SET executed_at WHERE id=? AND decision='approved'
        AND executed_at IS NULL AND exp > now``."""
        ...

    async def extend(self, approval_id: str, new_exp: datetime, now: datetime) -> bool:
        """Extend ``exp`` once (``extended`` flag) while still pending."""
        ...


def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


@dataclass
class ApprovalService:
    """Creates approvals for ``confirm`` decisions and applies the guarded transitions."""

    store: ApprovalStore
    new_id: NewId

    async def create(
        self,
        principal: Principal,
        tool: str,
        args: Mapping[str, object],
        target_uuids: Iterable[str],
        summary: str,
        now: datetime,
    ) -> tuple[ApprovalRecord, str]:
        """Insert a pending row bound to ``principal.profile_id`` and the args digest.

        Returns the record and the plaintext nonce (for the SSE event only). Approvals
        for a test-as call go to the actor (§3.1).
        """
        approver = principal.actor_profile_id or principal.profile_id
        if len(await self.store.pending_for(approver, now)) >= MAX_PENDING_PER_PROFILE:
            raise TooManyPendingError(approver)
        nonce = new_nonce()
        record = ApprovalRecord(
            id=self.new_id(),
            nonce_hash=hash_nonce(nonce),
            profile_id=approver,
            turn_id=principal.turn_id,
            tool=tool,
            args_digest=args_digest(tool, args, target_uuids),
            summary=summary,
            created_at=now,
            exp=now + APPROVAL_TTL,
        )
        await self.store.insert(record)
        return record, nonce

    async def decide(
        self,
        approval_id: str,
        profile_id: str,
        decision: ApprovalDecision,
        factor: Factor,
        now: datetime,
        nonce: str | None = None,
    ) -> ApprovalRecord | DecideError:
        """Decide once. ``nonce`` is required for the web factor and must hash-match."""
        record = await self.store.get(approval_id)
        if record is None:
            return "not_found"
        if record.profile_id != profile_id:
            return "not_yours"
        if nonce is not None and hash_nonce(nonce) != record.nonce_hash:
            return "bad_nonce"
        if factor == "web" and nonce is None:
            return "bad_nonce"
        if not record.pending:
            return "already_decided"
        if record.exp <= now:
            return "expired"
        if not await self.store.decide(approval_id, profile_id, decision, factor, now):
            return "already_decided"
        return replace(record, decision=decision, decided_at=now, factor=factor)

    async def consume(self, approval_id: str, now: datetime) -> ApprovalRecord | None:
        """Execute once: mark executed and return the record, or ``None`` if someone else did."""
        if not await self.store.mark_executed(approval_id, now):
            return None
        record = await self.store.get(approval_id)
        return None if record is None else replace(record, executed_at=now)

    async def extend_once(self, approval_id: str, now: datetime) -> bool:
        """The approver's session opened the stream in time: ``exp`` becomes created + 120 s."""
        record = await self.store.get(approval_id)
        if record is None or record.extended or not record.is_live(now):
            return False
        return await self.store.extend(approval_id, record.created_at + APPROVAL_EXTENDED_TTL, now)


class NewId(Protocol):
    def __call__(self) -> str: ...


class MemoryApprovalStore:
    """In-memory :class:`ApprovalStore` for tests and ``beepboop.testing``."""

    def __init__(self) -> None:
        self.rows: dict[str, ApprovalRecord] = {}

    async def insert(self, record: ApprovalRecord) -> None:
        self.rows[record.id] = record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.rows.get(approval_id)

    async def pending_for(self, profile_id: str, now: datetime) -> list[ApprovalRecord]:
        return [r for r in self.rows.values() if r.profile_id == profile_id and r.is_live(now)]

    async def decide(
        self,
        approval_id: str,
        profile_id: str,
        decision: ApprovalDecision,
        factor: Factor | None,
        now: datetime,
    ) -> bool:
        r = self.rows.get(approval_id)
        if r is None or r.profile_id != profile_id or not r.is_live(now):
            return False
        self.rows[approval_id] = replace(r, decision=decision, decided_at=now, factor=factor)
        return True

    async def mark_executed(self, approval_id: str, now: datetime) -> bool:
        r = self.rows.get(approval_id)
        if r is None or r.decision != "approved" or r.executed_at is not None or r.exp <= now:
            return False
        self.rows[approval_id] = replace(r, executed_at=now)
        return True

    async def extend(self, approval_id: str, new_exp: datetime, now: datetime) -> bool:
        r = self.rows.get(approval_id)
        if r is None or r.extended or not r.is_live(now):
            return False
        self.rows[approval_id] = replace(r, exp=new_exp, extended=True)
        return True
