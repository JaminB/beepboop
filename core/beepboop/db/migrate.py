"""Forward-only SQL migrations tracked with ``PRAGMA user_version`` (DATA_AND_PRIVACY §5).

``migrations/NNNN_<name>.sql`` run in order, one transaction each. Before the first
pending migration a ``VACUUM INTO backups/pre-migrate-<version>-<ts>.db`` snapshot is
written. A database whose ``user_version`` is ahead of the code refuses to migrate
(``SchemaAheadError``): core must not start on a downgraded image.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from beepboop.db.connect import user_version
from beepboop.log import get_logger

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
PRE_MIGRATE_KEEP_DAYS = 7


class MigrationError(RuntimeError):
    """A migration file is malformed or failed."""


class SchemaAheadError(MigrationError):
    """``user_version`` is newer than the code (downgrade after rollback, §5)."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every ``NNNN_name.sql`` in ``directory``, sorted, contiguous from 1."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration file name {path.name}")
        found.append(Migration(int(match.group(1)), match.group(2), path))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(f"migrations must be contiguous; got {migration.path.name}")
    return found


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    migrations = list_migrations(directory)
    return migrations[-1].version if migrations else 0


def snapshot_before_migrate(conn: sqlite3.Connection, backups_dir: Path, version: int) -> Path:
    """``VACUUM INTO backups/pre-migrate-<version>-<ts>.db`` (§5)."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"pre-migrate-{version:04d}-{int(time.time())}.db"
    conn.execute("VACUUM INTO ?", (str(target),))
    return target


def prune_pre_migrate_snapshots(
    backups_dir: Path, *, now: float | None = None, keep_days: int = PRE_MIGRATE_KEEP_DAYS
) -> int:
    """Delete ``pre-migrate-*.db`` older than ``keep_days``; returns the count."""
    if not backups_dir.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - keep_days * 86_400
    removed = 0
    for path in backups_dir.glob("pre-migrate-*.db"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def migrate(
    conn: sqlite3.Connection,
    *,
    directory: Path = MIGRATIONS_DIR,
    backups_dir: Path | None = None,
) -> list[int]:
    """Apply every pending migration; returns the versions applied.

    Runs on the writer's connection before the API binds. ``backups_dir=None`` skips
    the pre-migrate snapshot (tests, fresh files).
    """
    log = get_logger(__name__)
    migrations = list_migrations(directory)
    current = user_version(conn)
    target = migrations[-1].version if migrations else 0
    if current > target:
        raise SchemaAheadError(
            f"database schema is version {current} but this core only knows {target}; "
            "restore the pre-update snapshot (OPERATIONS §9)"
        )
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return []
    if backups_dir is not None and current > 0:
        snapshot = snapshot_before_migrate(conn, backups_dir, current)
        log.info("db.pre_migrate_snapshot", path=str(snapshot))
    applied: list[int] = []
    for migration in pending:
        # executescript() commits any open transaction first, so the transaction is part
        # of the script itself: one migration, one transaction (§5).
        script = "\n".join(
            (
                "BEGIN IMMEDIATE;",
                migration.sql(),
                f"PRAGMA user_version = {migration.version};",
                "COMMIT;",
            )
        )
        try:
            conn.executescript(script)
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {migration.path.name} failed: {exc}") from exc
        applied.append(migration.version)
        log.info("db.migrated", version=migration.version, name=migration.name)
    return applied
