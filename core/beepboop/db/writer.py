"""The single writer task (DATA_AND_PRIVACY §4).

One task owns the only read-write connection. Each write is a plain ``fn(conn)``
passed to :meth:`Writer.run`:

* ``durable=True`` jobs get their own ``BEGIN IMMEDIATE`` with ``synchronous=FULL``
  (profiles, logins, sessions, grants, approvals, secrets, voiceprints, keyring, audit).
* trace jobs (``durable=False``: turns, tool_calls, conversation, kv) are coalesced:
  up to ``batch_size`` jobs or ``batch_window_s`` seconds, one ``NORMAL`` transaction,
  each job inside its own SAVEPOINT so one failure does not sink the batch.

SQLite calls run in a worker thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from beepboop.log import get_logger

BATCH_SIZE: Final = 50
BATCH_WINDOW_S: Final = 0.25

type WriteFn[T] = Callable[[sqlite3.Connection], T]


class WriterClosedError(RuntimeError):
    """``run`` was called after ``close``."""


type JobMode = Literal["trace", "durable", "exclusive"]


@dataclass(slots=True)
class _Job:
    fn: Callable[[sqlite3.Connection], Any]
    mode: JobMode

    @property
    def durable(self) -> bool:
        return self.mode != "trace"

    future: asyncio.Future[Any] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


_STOP: Final = object()


class Writer:
    """Owns ``conn``; start with :meth:`start`, stop with :meth:`close`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        batch_size: int = BATCH_SIZE,
        batch_window_s: float = BATCH_WINDOW_S,
    ) -> None:
        self._conn = conn
        self._batch_size = batch_size
        self._batch_window = batch_window_s
        self._queue: asyncio.Queue[_Job | object] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._log = get_logger(__name__)

    # ------------------------------------------------------------- lifecycle --

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="db-writer")

    async def close(self) -> None:
        """Drain the queue, then stop. Later ``run`` calls raise :class:`WriterClosedError`."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            await self._queue.put(_STOP)
            await self._task
            self._task = None

    @property
    def connection(self) -> sqlite3.Connection:
        """The writer's connection. For migrations and tests only; writes go through ``run``."""
        return self._conn

    # ------------------------------------------------------------------ api --

    async def run[T](self, fn: WriteFn[T], *, durable: bool = False) -> T:
        """Queue ``fn`` and await its result. Exceptions propagate to the caller."""
        if self._closed:
            raise WriterClosedError
        if self._task is None:
            await self.start()
        return await self._submit(_Job(fn, "durable" if durable else "trace"))

    async def run_exclusive[T](self, fn: WriteFn[T]) -> T:
        """Run ``fn`` alone, outside any transaction (``VACUUM INTO``, ``wal_checkpoint``)."""
        if self._closed:
            raise WriterClosedError
        if self._task is None:
            await self.start()
        return await self._submit(_Job(fn, "exclusive"))

    async def _submit(self, job: _Job) -> Any:
        await self._queue.put(job)
        return await job.future

    # ----------------------------------------------------------------- loop --

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            job = item if isinstance(item, _Job) else None
            if job is None:  # pragma: no cover - only _STOP is not a _Job
                continue
            if job.durable:
                await self._run_durable(job)
                continue
            batch = [job]
            deadline = loop.time() + self._batch_window
            stop_after = False
            durable_next: _Job | None = None
            while len(batch) < self._batch_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._queue.get(), remaining)
                except TimeoutError:
                    break
                if nxt is _STOP:
                    stop_after = True
                    break
                nxt_job = nxt if isinstance(nxt, _Job) else None
                if nxt_job is None:  # pragma: no cover
                    continue
                if nxt_job.durable:
                    durable_next = nxt_job
                    break
                batch.append(nxt_job)
            await asyncio.to_thread(self._run_batch, batch)
            if durable_next is not None:
                await self._run_durable(durable_next)
            if stop_after:
                return

    async def _run_durable(self, job: _Job) -> None:
        try:
            if job.mode == "durable":
                result = await asyncio.to_thread(self._durable, job.fn)
            else:
                result = await asyncio.to_thread(job.fn, self._conn)
        except BaseException as exc:
            job.future.set_exception(exc)
        else:
            job.future.set_result(result)

    def _durable(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._conn
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA synchronous = NORMAL")
        return result

    def _run_batch(self, batch: list[_Job]) -> None:
        conn = self._conn
        conn.execute("BEGIN")
        # Futures resolve only after COMMIT: a caller that awaits db.write() and then
        # db.read() must see its own write (read-your-writes across the pool).
        outcomes: list[tuple[_Job, Any, BaseException | None]] = []
        try:
            for i, job in enumerate(batch):
                name = f"j{i}"
                conn.execute(f"SAVEPOINT {name}")
                try:
                    result = job.fn(conn)
                    conn.execute(f"RELEASE {name}")
                except BaseException as exc:
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                    outcomes.append((job, None, exc))
                else:
                    outcomes.append((job, result, None))
            conn.execute("COMMIT")
            for job, result, exc in outcomes:
                _resolve(job.future, result=result, exc=exc)
        except BaseException as exc:
            conn.execute("ROLLBACK")
            for job in batch:
                if not job.future.done():
                    _resolve(job.future, exc=exc)
            self._log.error("db.batch_failed", error=str(exc))


def _resolve(
    future: asyncio.Future[Any], *, result: Any = None, exc: BaseException | None = None
) -> None:
    loop = future.get_loop()
    if exc is not None:
        loop.call_soon_threadsafe(_set_exception, future, exc)
    else:
        loop.call_soon_threadsafe(_set_result, future, result)


def _set_result(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _set_exception(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)
