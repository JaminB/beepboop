"""``profiles`` and ``group_members`` (DATA_AND_PRIVACY §6.1). Durable."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Literal, cast

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import opt_int, opt_str
from beepboop.policy.types import GUEST_PROFILE_ID, ProfileKind, Risk

Verbosity = Literal["brief", "normal", "chatty"]


@dataclass(frozen=True, slots=True)
class ProfileRow:
    id: str
    name: str
    kind: ProfileKind
    risk_ceiling: Risk
    locale: str = "en-US"
    tts_voice: str | None = None
    speech_rate: float = 1.0
    verbosity: Verbosity = "normal"
    persona_override_json: str | None = None
    pin_hash_enc: bytes | None = None
    pin_failures: int = 0
    pin_locked_until: int | None = None
    transcript_retention_days: int | None = None
    disabled_at: int | None = None
    created_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ProfileRow:
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            kind=cast(ProfileKind, row["kind"]),
            risk_ceiling=cast(Risk, row["risk_ceiling"]),
            locale=str(row["locale"]),
            tts_voice=opt_str(row, "tts_voice"),
            speech_rate=float(row["speech_rate"]),
            verbosity=cast(Verbosity, row["verbosity"]),
            persona_override_json=opt_str(row, "persona_override_json"),
            pin_hash_enc=None if row["pin_hash_enc"] is None else bytes(row["pin_hash_enc"]),
            pin_failures=int(row["pin_failures"]),
            pin_locked_until=opt_int(row, "pin_locked_until"),
            transcript_retention_days=opt_int(row, "transcript_retention_days"),
            disabled_at=opt_int(row, "disabled_at"),
            created_at=int(row["created_at"]),
        )


class ProfileExistsError(ValueError):
    """A profile with that name already exists."""


def get(conn: sqlite3.Connection, profile_id: str) -> ProfileRow | None:
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return None if row is None else ProfileRow.from_row(row)


def by_name(conn: sqlite3.Connection, name: str) -> ProfileRow | None:
    """Exact name first, then case-insensitive (``--as alex`` finds "Alex")."""
    row = conn.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM profiles WHERE name = ? COLLATE NOCASE ORDER BY created_at LIMIT 1",
            (name,),
        ).fetchone()
    return None if row is None else ProfileRow.from_row(row)


def list_(conn: sqlite3.Connection) -> list[ProfileRow]:
    rows = conn.execute("SELECT * FROM profiles ORDER BY created_at, id").fetchall()
    return [ProfileRow.from_row(r) for r in rows]


def insert(
    conn: sqlite3.Connection,
    name: str,
    kind: ProfileKind,
    risk_ceiling: Risk,
    *,
    locale: str = "en-US",
    profile_id: str | None = None,
) -> ProfileRow:
    """Create a profile. Presets are applied by the caller (``policy.apply_preset``)."""
    if by_name(conn, name) is not None:
        raise ProfileExistsError(name)
    row = ProfileRow(
        id=profile_id or new_ulid(),
        name=name,
        kind=kind,
        risk_ceiling=risk_ceiling,
        locale=locale,
        created_at=now_ms(),
    )
    conn.execute(
        "INSERT INTO profiles (id, name, kind, risk_ceiling, locale, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (row.id, row.name, row.kind, row.risk_ceiling, row.locale, row.created_at),
    )
    return row


def update(conn: sqlite3.Connection, row: ProfileRow) -> None:
    """Write every mutable column of ``row`` (name, ceiling, voice, PIN state, retention,
    disabled).
    """
    conn.execute(
        "UPDATE profiles SET name = ?, kind = ?, risk_ceiling = ?, locale = ?, tts_voice = ?,"
        " speech_rate = ?, verbosity = ?, persona_override_json = ?, pin_hash_enc = ?,"
        " pin_failures = ?, pin_locked_until = ?, transcript_retention_days = ?, disabled_at = ?"
        " WHERE id = ?",
        (
            row.name,
            row.kind,
            row.risk_ceiling,
            row.locale,
            row.tts_voice,
            row.speech_rate,
            row.verbosity,
            row.persona_override_json,
            row.pin_hash_enc,
            row.pin_failures,
            row.pin_locked_until,
            row.transcript_retention_days,
            row.disabled_at,
            row.id,
        ),
    )


def set_disabled(conn: sqlite3.Connection, profile_id: str, disabled: bool) -> None:
    conn.execute(
        "UPDATE profiles SET disabled_at = ? WHERE id = ?",
        (now_ms() if disabled else None, profile_id),
    )


def set_pin(conn: sqlite3.Connection, profile_id: str, pin_hash_enc: bytes | None) -> None:
    """Set (or clear) the DEK-wrapped PIN hash and reset the failure counters."""
    conn.execute(
        "UPDATE profiles SET pin_hash_enc = ?, pin_failures = 0, pin_locked_until = NULL WHERE id"
        " ="
        " ?",
        (pin_hash_enc, profile_id),
    )


def delete(conn: sqlite3.Connection, profile_id: str) -> bool:
    """Raw row delete (cascades). The full §9.3 procedure is ``retention.delete_profile``."""
    if profile_id == GUEST_PROFILE_ID:
        msg = "the guest profile is never deleted"
        raise ValueError(msg)
    cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    return cur.rowcount > 0


# ------------------------------------------------------------------- groups --


def groups_for(conn: sqlite3.Connection, profile_id: str) -> frozenset[str]:
    rows = conn.execute(
        "SELECT group_id FROM group_members WHERE profile_id = ?", (profile_id,)
    ).fetchall()
    return frozenset(str(r["group_id"]) for r in rows)


def add_to_group(conn: sqlite3.Connection, profile_id: str, group_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO group_members (group_id, profile_id) VALUES (?, ?)",
        (group_id, profile_id),
    )


def remove_from_group(conn: sqlite3.Connection, profile_id: str, group_id: str) -> None:
    conn.execute(
        "DELETE FROM group_members WHERE group_id = ? AND profile_id = ?", (group_id, profile_id)
    )


def members(conn: sqlite3.Connection, group_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT profile_id FROM group_members WHERE group_id = ? ORDER BY profile_id", (group_id,)
    ).fetchall()
    return [str(r["profile_id"]) for r in rows]


def with_pin_failures(row: ProfileRow, failures: int, locked_until: int | None) -> ProfileRow:
    return replace(row, pin_failures=failures, pin_locked_until=locked_until)
