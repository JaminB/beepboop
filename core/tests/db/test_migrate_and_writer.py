"""Migrations (DATA_AND_PRIVACY §5), connection settings (§3) and the writer (§4)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from beepboop.db import (
    Database,
    SchemaAheadError,
    Writer,
    WriterClosedError,
    connect,
    latest_version,
    list_migrations,
    migrate,
    prune_pre_migrate_snapshots,
    user_version,
)

EXPECTED_TABLES = {
    "household", "profiles", "logins", "sessions", "invites", "groups", "group_members", "grants",
    "ha_entities", "voiceprints", "voice_samples", "integrations", "keyring", "secrets", "turns",
    "tool_calls", "approvals", "conversation", "kv", "audit",
}  # fmt: skip


def test_migrations_are_contiguous_and_apply(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    assert [m.version for m in list_migrations()] == list(range(1, latest_version() + 1))
    applied = migrate(conn)
    assert applied == list(range(1, latest_version() + 1))
    assert user_version(conn) == latest_version()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    assert migrate(conn) == []  # idempotent
    guest = conn.execute("SELECT kind, risk_ceiling FROM profiles WHERE id='guest'").fetchone()
    assert tuple(guest) == ("guest", "act")
    assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 3


def test_pragmas_applied(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    migrate(conn)
    ro = connect(tmp_path / "x.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO household VALUES ('k', '1', 0)")


def test_schema_ahead_refuses(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    conn.execute("PRAGMA user_version = 999")
    with pytest.raises(SchemaAheadError):
        migrate(conn)


def test_pre_migrate_snapshot_and_prune(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    conn.execute("PRAGMA user_version = 0")
    backups = tmp_path / "backups"
    migrate(conn, backups_dir=backups)  # fresh file: no snapshot at version 0
    assert not list(backups.glob("*")) if backups.exists() else True
    # Simulate an older schema that needs a migration: version 0 with no tables is the only
    # case we can construct without a second migration file, so snapshot directly.
    from beepboop.db.migrate import snapshot_before_migrate

    snap = snapshot_before_migrate(conn, backups, 1)
    assert snap.exists()
    assert prune_pre_migrate_snapshots(backups, now=snap.stat().st_mtime + 8 * 86_400) == 1


def test_foreign_keys_cascade(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO profiles (id, name, kind, risk_ceiling, created_at) VALUES"
        " ('p','P','member','critical',0)"
    )
    conn.execute("INSERT INTO kv VALUES ('m','p','k','1',0)")
    conn.execute("DELETE FROM profiles WHERE id='p'")
    assert conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 0


# ------------------------------------------------------------------ writer --


async def test_writer_batches_trace_and_isolates_failures(tmp_path: Path) -> None:
    conn = connect(tmp_path / "w.db")
    migrate(conn)
    writer = Writer(conn, batch_window_s=0.05)
    await writer.start()

    def ok(key: str):
        def _fn(c: sqlite3.Connection) -> str:
            c.execute("INSERT INTO household VALUES (?, '1', 0)", (key,))
            return key

        return _fn

    def bad(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO nope VALUES (1)")

    results = await asyncio.gather(
        writer.run(ok("a")), writer.run(bad), writer.run(ok("b")), return_exceptions=True
    )
    assert results[0] == "a"
    assert results[2] == "b"
    assert isinstance(results[1], sqlite3.OperationalError)
    assert conn.execute("SELECT COUNT(*) FROM household").fetchone()[0] == 2
    await writer.close()
    with pytest.raises(WriterClosedError):
        await writer.run(ok("c"))


async def test_writer_durable_runs_in_own_transaction(tmp_path: Path) -> None:
    conn = connect(tmp_path / "w.db")
    migrate(conn)
    writer = Writer(conn, batch_window_s=0.0)

    def durable(c: sqlite3.Connection) -> int:
        c.execute("INSERT INTO household VALUES ('d', '1', 0)")
        return int(c.execute("PRAGMA synchronous").fetchone()[0])

    assert await writer.run(durable, durable=True) == 2  # FULL inside the job
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # back to NORMAL
    with pytest.raises(sqlite3.OperationalError):
        await writer.run(lambda c: c.execute("INSERT INTO nope VALUES (1)"), durable=True)
    await writer.close()


async def test_database_read_write_and_snapshot(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "db.db", batch_window_s=0.0)
    await db.write(lambda c: c.execute("INSERT INTO household VALUES ('k', '\"v\"', 0)"))
    assert (
        await db.read(lambda c: c.execute("SELECT value_json FROM household").fetchone()[0])
        == '"v"'
    )
    assert await db.schema_version() == latest_version()
    snap = await db.vacuum_into(tmp_path / "backups" / "snap.db")
    assert snap.exists()
    await db.checkpoint()
    await db.close()


def test_guest_preset_rows_match_the_yaml(tmp_path: Path) -> None:
    """Migration 0002 applies ``presets/guest.yaml`` to the built-in guest (IDENTITY §9,
    DATA_AND_PRIVACY §8): the two must say the same thing."""
    from beepboop.db import repo
    from beepboop.db.connect import connect
    from beepboop.policy.presets import load_preset

    conn = connect(tmp_path / "g.db")
    migrate(conn)
    rows = [g for g in repo.grants.list_all(conn) if g.subject_id == "guest"]
    assert {(g.resource_type, g.resource_id, g.max_risk) for g in rows} == {
        (g.resource_type, g.resource_id, g.max_risk) for g in load_preset("guest").grants
    }
    assert all(g.source == "preset:guest" and g.subject_type == "profile" for g in rows)
    conn.close()
