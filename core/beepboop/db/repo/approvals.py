"""``approvals`` (DATA_AND_PRIVACY §6.1, IDENTITY_AND_TRUST §8.2). Durable, ``synchronous=FULL``.

``DbApprovalStore`` is the production :class:`beepboop.policy.ApprovalStore`: the two
transitions are single guarded ``UPDATE``s whose rowcount decides the outcome.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from beepboop.db.ids import from_ms, to_ms
from beepboop.db.repo._rows import opt_int, opt_str
from beepboop.policy.approvals import ApprovalDecision, ApprovalRecord
from beepboop.policy.types import Factor

if TYPE_CHECKING:
    from beepboop.db.database import Database


def _to_record(row: sqlite3.Row) -> ApprovalRecord:
    decided = opt_int(row, "decided_at")
    executed = opt_int(row, "executed_at")
    return ApprovalRecord(
        id=str(row["id"]),
        nonce_hash=str(row["nonce_hash"]),
        profile_id=opt_str(row, "profile_id"),
        turn_id=opt_str(row, "turn_id"),
        tool=str(row["tool"]),
        args_digest=str(row["args_digest"]),
        summary=str(row["summary"]),
        created_at=from_ms(int(row["created_at"])),
        exp=from_ms(int(row["exp"])),
        decision=cast(ApprovalDecision | None, row["decision"]),
        decided_at=None if decided is None else from_ms(decided),
        executed_at=None if executed is None else from_ms(executed),
        factor=cast(Factor | None, row["factor"]),
        extended=bool(row["extended"]),
    )


def insert(conn: sqlite3.Connection, r: ApprovalRecord) -> None:
    conn.execute(
        "INSERT INTO approvals (id, nonce_hash, profile_id, turn_id, tool, args_digest, summary,"
        " created_at, exp, decision, decided_at, executed_at, factor, extended)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r.id,
            r.nonce_hash,
            r.profile_id,
            r.turn_id,
            r.tool,
            r.args_digest,
            r.summary,
            to_ms(r.created_at),
            to_ms(r.exp),
            r.decision,
            None if r.decided_at is None else to_ms(r.decided_at),
            None if r.executed_at is None else to_ms(r.executed_at),
            r.factor,
            int(r.extended),
        ),
    )


def get(conn: sqlite3.Connection, approval_id: str) -> ApprovalRecord | None:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return None if row is None else _to_record(row)


def pending_for(conn: sqlite3.Connection, profile_id: str, now: datetime) -> list[ApprovalRecord]:
    rows = conn.execute(
        "SELECT * FROM approvals WHERE profile_id = ? AND decision IS NULL AND exp > ?"
        " ORDER BY created_at",
        (profile_id, to_ms(now)),
    ).fetchall()
    return [_to_record(r) for r in rows]


def decide(
    conn: sqlite3.Connection,
    approval_id: str,
    profile_id: str,
    decision: ApprovalDecision,
    factor: Factor | None,
    now: datetime,
) -> bool:
    """§8.2 "Decide, once"."""
    cur = conn.execute(
        "UPDATE approvals SET decision = ?, decided_at = ?, factor = ?"
        " WHERE id = ? AND profile_id = ? AND decision IS NULL AND exp > ?",
        (decision, to_ms(now), factor, approval_id, profile_id, to_ms(now)),
    )
    return cur.rowcount == 1


def mark_executed(conn: sqlite3.Connection, approval_id: str, now: datetime) -> bool:
    """§8.2 "Execute, once"."""
    cur = conn.execute(
        "UPDATE approvals SET executed_at = ? WHERE id = ? AND decision = 'approved'"
        " AND executed_at IS NULL AND exp > ?",
        (to_ms(now), approval_id, to_ms(now)),
    )
    return cur.rowcount == 1


def extend(conn: sqlite3.Connection, approval_id: str, new_exp: datetime, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE approvals SET exp = ?, extended = 1 WHERE id = ? AND decision IS NULL"
        " AND extended = 0 AND exp > ?",
        (to_ms(new_exp), approval_id, to_ms(now)),
    )
    return cur.rowcount == 1


def expire_pending(conn: sqlite3.Connection, now: datetime) -> int:
    """Mark every pending row past ``exp`` as ``expired`` (the loop and nightly job call this)."""
    return conn.execute(
        "UPDATE approvals SET decision = 'expired', decided_at = ?"
        " WHERE decision IS NULL AND exp <= ?",
        (to_ms(now), to_ms(now)),
    ).rowcount


def pseudonymise_for_profile(conn: sqlite3.Connection, profile_id: str, now: datetime) -> int:
    """§9.3 steps 2 and 9: deny pending, then drop the name; rows are kept 365 days."""
    conn.execute(
        "UPDATE approvals SET decision = 'denied', decided_at = ?"
        " WHERE profile_id = ? AND decision IS NULL",
        (to_ms(now), profile_id),
    )
    return conn.execute(
        "UPDATE approvals SET profile_id = NULL WHERE profile_id = ?", (profile_id,)
    ).rowcount


@dataclass
class DbApprovalStore:
    """The production ``ApprovalStore`` over :class:`Database`."""

    db: Database

    async def insert(self, record: ApprovalRecord) -> None:
        await self.db.write(lambda c: insert(c, record), durable=True)

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return await self.db.read(lambda c: get(c, approval_id))

    async def pending_for(self, profile_id: str, now: datetime) -> list[ApprovalRecord]:
        return await self.db.read(lambda c: pending_for(c, profile_id, now))

    async def decide(
        self,
        approval_id: str,
        profile_id: str,
        decision: ApprovalDecision,
        factor: Factor | None,
        now: datetime,
    ) -> bool:
        return await self.db.write(
            lambda c: decide(c, approval_id, profile_id, decision, factor, now), durable=True
        )

    async def mark_executed(self, approval_id: str, now: datetime) -> bool:
        return await self.db.write(lambda c: mark_executed(c, approval_id, now), durable=True)

    async def extend(self, approval_id: str, new_exp: datetime, now: datetime) -> bool:
        return await self.db.write(lambda c: extend(c, approval_id, new_exp, now), durable=True)
