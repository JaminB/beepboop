"""Retention hooks (DATA_AND_PRIVACY §7) and the per-person delete (§9.3).

Each function is one writer job (``durable=True``) and returns counts for the
``retention.purge`` audit row. The nightly scheduler (03:00 local) calls
:func:`nightly_purge`; the API calls :func:`delete_profile` and :func:`delete_turn`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from beepboop.db.ids import to_ms
from beepboop.db.repo import (
    approvals,
    audit,
    conversation,
    grants,
    ha_entities,
    household,
    logins,
    profiles,
    sessions,
)
from beepboop.policy.types import GUEST_PROFILE_ID

SAFETY_RECORD_DAYS = 365
DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class PurgeCounts:
    turns: int = 0
    tool_calls: int = 0
    safety_records: int = 0
    approvals_expired: int = 0
    approvals: int = 0
    conversations: int = 0
    sessions: int = 0
    invites: int = 0
    grants: int = 0
    entities: int = 0
    audit: int = 0
    guest_kv: int = 0

    def as_dict(self) -> dict[str, object]:
        return dict(asdict(self))


def _int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    value = household.get(conn, key)
    return int(value) if isinstance(value, int | float) else default


def nightly_purge(conn: sqlite3.Connection, now: datetime) -> PurgeCounts:
    """Apply every §7 default. Safety records are pseudonymised at purge time and kept 365 days."""
    now_ms = to_ms(now)
    default_days = _int_setting(conn, "transcript_retention_days", 30)
    guest_days = _int_setting(conn, "guest_turn_retention_days", 7)
    audit_days = _int_setting(conn, "audit_retention_days", 365)

    turns_deleted = 0
    calls_deleted = 0
    safety_kept = 0
    for p in profiles.list_(conn):
        days = (
            guest_days
            if p.id == GUEST_PROFILE_ID
            else (
                default_days if p.transcript_retention_days is None else p.transcript_retention_days
            )
        )
        cutoff = now_ms - days * DAY_MS
        old = [
            str(r["id"])
            for r in conn.execute(
                "SELECT id FROM turns WHERE profile_id = ? AND created_at < ?", (p.id, cutoff)
            )
        ]
        for turn_id in old:
            # Ordinary tool calls go with the turn; safety records are detached and pseudonymised.
            safety = conn.execute(
                "UPDATE tool_calls SET profile_id = NULL WHERE turn_id = ?"
                " AND (effective_risk = 'critical' OR decision = 'confirm')",
                (turn_id,),
            ).rowcount
            safety_kept += safety
            calls_deleted += conn.execute(
                "DELETE FROM tool_calls WHERE turn_id = ?"
                " AND NOT (effective_risk = 'critical' OR decision = 'confirm')",
                (turn_id,),
            ).rowcount
            if safety:
                # Keep a skeleton turn so the FK holds; strip everything personal.
                conn.execute(
                    "UPDATE turns SET transcript = NULL, reply = NULL, profile_id = ? WHERE id = ?",
                    (GUEST_PROFILE_ID, turn_id),
                )
            else:
                conn.execute("DELETE FROM turns WHERE id = ?", (turn_id,))
                turns_deleted += 1

    safety_cutoff = now_ms - SAFETY_RECORD_DAYS * DAY_MS
    conn.execute(
        "DELETE FROM tool_calls WHERE created_at < ? AND (effective_risk = 'critical' OR decision"
        " ="
        " 'confirm')",
        (safety_cutoff,),
    )
    conn.execute(
        "DELETE FROM turns WHERE created_at < ? AND id NOT IN (SELECT turn_id FROM tool_calls)",
        (safety_cutoff,),
    )
    # DATA_AND_PRIVACY §8: a guest's module data (a timer it set) lives no longer than its
    # turns; sweep kv rows under the guest profile with the guest retention.
    guest_kv = conn.execute(
        "DELETE FROM kv WHERE profile_id = ? AND updated_at < ?",
        (GUEST_PROFILE_ID, now_ms - guest_days * DAY_MS),
    ).rowcount
    approvals_expired = approvals.expire_pending(conn, now)
    approvals_purged = conn.execute(
        "DELETE FROM approvals WHERE created_at < ?", (safety_cutoff,)
    ).rowcount

    counts = PurgeCounts(
        turns=turns_deleted,
        tool_calls=calls_deleted,
        safety_records=safety_kept,
        approvals_expired=approvals_expired,
        approvals=approvals_purged,
        conversations=conversation.purge_expired(conn, now_ms),
        sessions=sessions.purge_expired(conn, now_ms),
        invites=logins.purge_invites(conn, now_ms),
        grants=grants.purge_expired(conn, now.isoformat()),
        entities=ha_entities.purge_removed(conn, now_ms),
        audit=audit.purge_older_than(conn, now_ms - audit_days * DAY_MS),
        guest_kv=guest_kv,
    )
    audit.record(
        conn,
        actor_id=None,
        actor_name="beepboop",
        via="system",
        action="retention.purge",
        details=counts.as_dict(),
        at=now_ms,
    )
    return counts


class LastAdminError(ValueError):
    """Refused: this is the last admin (§9.3)."""


def delete_profile(
    conn: sqlite3.Connection, profile_id: str, *, acting_admin: str | None, now: datetime
) -> None:
    """ "Delete me" (§9.3), as one durable job. Raises for ``guest`` and the last admin.

    The FK cascades do most of the work (sessions, invites, turns, conversation, kv,
    integrations, secrets, voice tables, group_members); this function handles what
    cascades cannot: pseudonymised safety records, approvals, audit and polymorphic grants.
    """
    if profile_id == GUEST_PROFILE_ID:
        msg = "the guest profile is never deleted"
        raise ValueError(msg)
    row = profiles.get(conn, profile_id)
    if row is None:
        return
    if row.kind == "admin":
        admins = [p for p in profiles.list_(conn) if p.kind == "admin"]
        if len(admins) <= 1:
            raise LastAdminError(profile_id)
    now_ms = to_ms(now)
    # 2 + 9: approvals -> denied, then pseudonymised.
    approvals.pseudonymise_for_profile(conn, profile_id, now)
    # 3 + 9: safety records survive the turn cascade by moving to a skeleton turn under guest.
    safety_turns = [
        str(r["turn_id"])
        for r in conn.execute(
            "SELECT DISTINCT turn_id FROM tool_calls WHERE profile_id = ?"
            " AND (effective_risk = 'critical' OR decision = 'confirm')",
            (profile_id,),
        )
    ]
    for turn_id in safety_turns:
        conn.execute(
            "DELETE FROM tool_calls WHERE turn_id = ? AND NOT (effective_risk = 'critical' OR"
            " decision ="
            " 'confirm')",
            (turn_id,),
        )
        conn.execute("UPDATE tool_calls SET profile_id = NULL WHERE turn_id = ?", (turn_id,))
        conn.execute(
            "UPDATE turns SET transcript = NULL, reply = NULL, profile_id = ?, actor_profile_id ="
            " NULL"
            " WHERE id = ?",
            (GUEST_PROFILE_ID, turn_id),
        )
    # 7: polymorphic grants have no FK.
    grants.delete_for_subject(conn, "profile", profile_id)
    # 8: audit kept, actor pseudonymised.
    audit.pseudonymise_actor(conn, profile_id)
    # 1, 4, 5, 6, 10: the cascade.
    profiles.delete(conn, profile_id)
    # 11.
    audit.record(
        conn,
        actor_id=acting_admin,
        actor_name=acting_admin or "beepboop",
        via="web" if acting_admin else "system",
        action="delete_profile",
        target=profile_id,
        at=now_ms,
    )


def delete_turn(conn: sqlite3.Connection, turn_id: str) -> bool:
    """ "Delete this turn" (§9.3): transcript and reply go, ordinary calls go, safety records
    stay.
    """
    from beepboop.db.repo import turns

    return turns.delete_turn(conn, turn_id)


def pre_update_snapshot_name(now: datetime) -> str:
    """``backups/pre-update-<ts>/`` directory name (§11)."""
    return f"pre-update-{now.strftime('%Y%m%dT%H%M%SZ')}"


def nightly_schedule() -> dict[str, timedelta]:
    """The §11 nightly offsets from local midnight, for the scheduler."""
    return {
        "retention.purge": timedelta(hours=3),
        "centroids.recompute": timedelta(hours=3, minutes=10),
        "backup": timedelta(hours=3, minutes=30),
    }
