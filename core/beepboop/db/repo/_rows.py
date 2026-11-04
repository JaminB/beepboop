"""Helpers for turning ``sqlite3.Row`` into typed values."""

from __future__ import annotations

import json
import sqlite3
from typing import cast


def opt_str(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def opt_int(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    return None if value is None else int(value)


def opt_float(row: sqlite3.Row, key: str) -> float | None:
    value = row[key]
    return None if value is None else float(value)


def json_obj(raw: str | None) -> dict[str, object]:
    """Parse a ``*_json`` object column; ``None`` → ``{}``."""
    if raw is None:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        msg = "expected a JSON object"
        raise ValueError(msg)
    return cast(dict[str, object], value)


def dump_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
