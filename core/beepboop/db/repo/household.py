"""``household``: one row per settings key (DATA_AND_PRIVACY §6.1). Durable."""

from __future__ import annotations

import json
import sqlite3

from beepboop.db.ids import now_ms
from beepboop.db.repo._rows import dump_json

#: Keys core itself reads, with their defaults (DATA_AND_PRIVACY §7, ARCHITECTURE §9).
DEFAULTS: dict[str, object] = {
    "name": "Home",
    "locale": "en-US",
    "timezone": "UTC",
    "transcript_retention_days": 30,
    "guest_turn_retention_days": 7,
    "audit_retention_days": 365,
}


def get(conn: sqlite3.Connection, key: str) -> object | None:
    row = conn.execute("SELECT value_json FROM household WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
    return json.loads(row["value_json"])


def set_(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO household (key, value_json, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
        " updated_at = excluded.updated_at",
        (key, dump_json(value), now_ms()),
    )


def all_(conn: sqlite3.Connection) -> dict[str, object]:
    out = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value_json FROM household"):
        out[str(row["key"])] = json.loads(row["value_json"])
    return out
