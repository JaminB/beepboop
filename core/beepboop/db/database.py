"""``Database``: the writer plus a pool of read-only connections (DATA_AND_PRIVACY §4).

Open once per process::

    db = await Database.open(data_dir / "beepboop.db")
    row = await db.read(lambda c: profiles.get(c, "01J…"))
    await db.write(lambda c: grants.insert(c, grant), durable=True)
    await db.close()

Repositories in :mod:`beepboop.db.repo` are plain functions over a connection, so
they are testable without the queue and composable inside one job.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self

from beepboop.db.connect import connect, user_version
from beepboop.db.migrate import MIGRATIONS_DIR, migrate
from beepboop.db.writer import Writer

READ_POOL_SIZE = 4

type ReadFn[T] = Callable[[sqlite3.Connection], T]


class Database:
    """One file, one writer, four readers. Use :meth:`open`."""

    def __init__(self, path: Path, writer: Writer, readers: list[sqlite3.Connection]) -> None:
        self.path = path
        self._writer = writer
        self._readers: asyncio.Queue[sqlite3.Connection] = asyncio.Queue()
        self._all_readers = readers
        for r in readers:
            self._readers.put_nowait(r)

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        migrate_schema: bool = True,
        backups_dir: Path | None = None,
        migrations_dir: Path = MIGRATIONS_DIR,
        read_pool: int = READ_POOL_SIZE,
        batch_window_s: float | None = None,
    ) -> Database:
        """Open (creating if needed), migrate forward, start the writer."""

        def _open() -> tuple[sqlite3.Connection, list[sqlite3.Connection]]:
            rw = connect(path)
            if migrate_schema:
                migrate(rw, directory=migrations_dir, backups_dir=backups_dir)
            ro = [connect(path, readonly=True) for _ in range(read_pool)]
            return rw, ro

        rw, ro = await asyncio.to_thread(_open)
        writer = Writer(rw) if batch_window_s is None else Writer(rw, batch_window_s=batch_window_s)
        await writer.start()
        return cls(path, writer, ro)

    async def close(self) -> None:
        await self._writer.close()

        def _close_all() -> None:
            for r in self._all_readers:
                r.close()
            self._writer.connection.close()

        await asyncio.to_thread(_close_all)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------ api --

    async def write[T](self, fn: Callable[[sqlite3.Connection], T], *, durable: bool = False) -> T:
        """Run ``fn`` on the writer. ``durable`` = ``synchronous=FULL``, own transaction."""
        return await self._writer.run(fn, durable=durable)

    async def read[T](self, fn: ReadFn[T]) -> T:
        """Run ``fn`` on a pooled read-only connection, in a worker thread."""
        conn = await self._readers.get()
        try:
            return await asyncio.to_thread(fn, conn)
        finally:
            self._readers.put_nowait(conn)

    async def schema_version(self) -> int:
        return await self.read(user_version)

    async def checkpoint(self) -> None:
        """``PRAGMA wal_checkpoint(TRUNCATE)``; the nightly job calls it before backup (§11)."""
        await self._writer.run_exclusive(
            lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        )

    async def vacuum_into(self, target: Path) -> Path:
        """Online consistent snapshot (§11 step 2). Runs as one durable job."""

        def _vacuum(conn: sqlite3.Connection) -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            conn.execute("VACUUM INTO ?", (str(target),))
            return target

        return await self._writer.run_exclusive(_vacuum)

    @property
    def writer(self) -> Writer:
        return self._writer
