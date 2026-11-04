"""SQLite: schema, migrations, the single writer task, repositories and retention.

docs/DATA_AND_PRIVACY.md owns the DDL (§6), the settings (§3), the writer pattern (§4),
migrations (§5), retention (§7) and delete (§9). Open the database once per process::

    from beepboop.db import Database, repo

    db = await Database.open(data_dir / "beepboop.db", backups_dir=data_dir / "backups")
    profile = await db.read(lambda c: repo.profiles.get(c, "01J…"))
    await db.write(lambda c: repo.grants.insert(c, grant), durable=True)
"""

from beepboop.db import repo, retention
from beepboop.db.connect import PRAGMAS, connect, user_version
from beepboop.db.database import Database
from beepboop.db.ids import from_ms, new_ulid, now_ms, to_ms
from beepboop.db.migrate import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    SchemaAheadError,
    latest_version,
    list_migrations,
    migrate,
    prune_pre_migrate_snapshots,
)
from beepboop.db.writer import Writer, WriterClosedError

__all__ = [
    "MIGRATIONS_DIR",
    "PRAGMAS",
    "Database",
    "Migration",
    "MigrationError",
    "SchemaAheadError",
    "Writer",
    "WriterClosedError",
    "connect",
    "from_ms",
    "latest_version",
    "list_migrations",
    "migrate",
    "new_ulid",
    "now_ms",
    "prune_pre_migrate_snapshots",
    "repo",
    "retention",
    "to_ms",
    "user_version",
]
