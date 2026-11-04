"""``kv``: the only persistence a module gets (DATA_AND_PRIVACY §6.1, MODULES §5.3). Trace writes.

Invariants enforced here: values are JSON ≤ 64 KiB; ≤ 1 MiB per (module, profile).
"""

from __future__ import annotations

import json
import sqlite3

from beepboop.db.ids import now_ms

MAX_VALUE_BYTES = 64 * 1024
MAX_SCOPE_BYTES = 1024 * 1024


class KVLimitError(ValueError):
    """A value or a scope exceeded its size limit."""


def _scope(profile_id: str | None) -> str:
    return profile_id or ""


def get(
    conn: sqlite3.Connection, module_id: str, profile_id: str | None, key: str
) -> object | None:
    row = conn.execute(
        "SELECT value_json FROM kv WHERE module_id = ? AND COALESCE(profile_id, '') = ? AND key ="
        " ?",
        (module_id, _scope(profile_id), key),
    ).fetchone()
    return None if row is None else json.loads(row["value_json"])


def set_(
    conn: sqlite3.Connection, module_id: str, profile_id: str | None, key: str, value: object
) -> None:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    size = len(encoded.encode())
    if size > MAX_VALUE_BYTES:
        msg = f"kv value for {key!r} is {size} bytes; limit is {MAX_VALUE_BYTES}"
        raise KVLimitError(msg)
    current = scope_bytes(conn, module_id, profile_id, exclude_key=key)
    if current + size > MAX_SCOPE_BYTES:
        msg = f"kv scope ({module_id}, {profile_id}) would exceed {MAX_SCOPE_BYTES} bytes"
        raise KVLimitError(msg)
    conn.execute(
        "INSERT INTO kv (module_id, profile_id, key, value_json, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(module_id, COALESCE(profile_id, ''), key) DO UPDATE SET"
        " value_json = excluded.value_json, updated_at = excluded.updated_at",
        (module_id, profile_id, key, encoded, now_ms()),
    )


def delete(conn: sqlite3.Connection, module_id: str, profile_id: str | None, key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM kv WHERE module_id = ? AND COALESCE(profile_id, '') = ? AND key = ?",
        (module_id, _scope(profile_id), key),
    )
    return cur.rowcount > 0


def keys(
    conn: sqlite3.Connection, module_id: str, profile_id: str | None, prefix: str = ""
) -> list[str]:
    rows = conn.execute(
        "SELECT key FROM kv WHERE module_id = ? AND COALESCE(profile_id, '') = ? ORDER BY key",
        (module_id, _scope(profile_id)),
    ).fetchall()
    return [str(r["key"]) for r in rows if str(r["key"]).startswith(prefix)]


def scope_bytes(
    conn: sqlite3.Connection,
    module_id: str,
    profile_id: str | None,
    *,
    exclude_key: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(value_json AS BLOB))), 0) AS n FROM kv"
        " WHERE module_id = ? AND COALESCE(profile_id, '') = ? AND (? IS NULL OR key != ?)",
        (module_id, _scope(profile_id), exclude_key, exclude_key),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def delete_module(conn: sqlite3.Connection, module_id: str, profile_id: str | None = None) -> int:
    """Drop a module's rows: one scope, or every scope when ``profile_id`` is ``None``."""
    if profile_id is None:
        return conn.execute("DELETE FROM kv WHERE module_id = ?", (module_id,)).rowcount
    return conn.execute(
        "DELETE FROM kv WHERE module_id = ? AND profile_id = ?", (module_id, profile_id)
    ).rowcount
