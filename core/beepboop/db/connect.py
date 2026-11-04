"""Open SQLite connections with the DATA_AND_PRIVACY §3 settings.

One read-write connection (the writer's) and ``file:…?mode=ro`` readers. Every
connection gets the same PRAGMAs; ``synchronous`` is raised to FULL per durable
transaction by the writer (§4), never globally.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Set on every connection (§3). ``journal_mode`` persists in the file; the rest are per connection.
PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA secure_delete = ON",
    "PRAGMA wal_autocheckpoint = 1000",
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA mmap_size = 134217728",
)
#: Read-only connections cannot change persistent settings; this subset is enough.
READER_PRAGMAS: tuple[str, ...] = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 134217728",
)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open ``path`` with autocommit off (``isolation_level=None``: explicit BEGIN/COMMIT).

    ``check_same_thread=False`` because the writer task runs statements in a worker
    thread; the writer serialises access itself.
    """
    if readonly:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        pragmas = READER_PRAGMAS
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        pragmas = PRAGMAS
    conn.row_factory = sqlite3.Row
    for pragma in pragmas:
        conn.execute(pragma)
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    """``PRAGMA user_version`` = the highest applied migration number (§5)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0
