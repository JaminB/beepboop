"""The core scheduler: the nightly jobs of DATA_AND_PRIVACY §11, in the household timezone.

Jobs are plain async callables with a local wall-clock time (and optionally a weekday).
One task sleeps until the soonest job, runs it, logs the outcome and moves on; a job that
raises is logged and does not stop the others. Heavy work inside a job (``VACUUM INTO``,
the tar, ``age``) already runs in worker threads, never on the event loop.

:func:`nightly_jobs` builds the v0.1 set from the offsets in
:func:`beepboop.db.retention.nightly_schedule` (03:00 purge, 03:30 backup) plus the
weekly restore check (Sunday 04:00).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from beepboop.log import get_logger

type JobFn = Callable[[datetime], Awaitable[object]]

#: Weekly restore check: Sunday (``date.weekday() == 6``) at 04:00 local.
RESTORE_CHECK_WEEKDAY = 6
RESTORE_CHECK_AT = time(4, 0)


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    at: time
    run: JobFn
    weekday: int | None = None  # 0 = Monday … 6 = Sunday; None = daily


def next_fire(job: Job, after: datetime, tz: tzinfo) -> datetime:
    """The first local ``job.at`` strictly after ``after`` (aware), as an aware UTC time."""
    local = after.astimezone(tz)
    day: date = local.date()
    for offset in range(8):
        d = day + timedelta(days=offset)
        if job.weekday is not None and d.weekday() != job.weekday:
            continue
        candidate = datetime.combine(d, job.at, tzinfo=tz)
        if candidate > local:
            return candidate.astimezone(UTC)
    msg = f"no fire time for {job.name}"  # pragma: no cover - 8 days always contain one
    raise RuntimeError(msg)


class Scheduler:
    """Runs :class:`Job` s forever. ``clock``/``sleep`` are injectable for tests."""

    def __init__(
        self,
        jobs: list[Job],
        tz: tzinfo,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.jobs = jobs
        self.tz = tz
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self.history: list[tuple[str, datetime, str]] = []

    def upcoming(self) -> list[tuple[datetime, Job]]:
        now = self._clock()
        return sorted(((next_fire(j, now, self.tz), j) for j in self.jobs), key=lambda x: x[0])

    async def run_once(self) -> tuple[Job, str]:
        """Sleep until the soonest job, run it, return (job, outcome)."""
        when, job = self.upcoming()[0]
        delay = (when - self._clock()).total_seconds()
        if delay > 0:
            await self._sleep(delay)
        log = get_logger(__name__)
        try:
            await job.run(when)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("scheduler.job_failed", job=job.name, error=type(exc).__name__)
            outcome = "error"
        else:
            log.info("scheduler.job_done", job=job.name)
            outcome = "ok"
        self.history.append((job.name, when, outcome))
        del self.history[:-50]
        return job, outcome

    async def run_forever(self) -> None:
        if not self.jobs:
            return
        while True:
            await self.run_once()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="beepboop.scheduler")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def daily_at(offset: timedelta) -> time:
    """A ``nightly_schedule()`` offset from local midnight as a wall-clock time."""
    minutes = int(offset.total_seconds() // 60)
    return time(minutes // 60, minutes % 60)


def nightly_jobs(
    *,
    purge: JobFn,
    backup: JobFn | None,
    restore_check: JobFn | None,
) -> list[Job]:
    """The v0.1 set. Backup jobs are omitted while secrets are unavailable (no KEK)."""
    from beepboop.db.retention import nightly_schedule

    offsets = nightly_schedule()
    jobs = [Job("retention.purge", daily_at(offsets["retention.purge"]), purge)]
    if backup is not None:
        jobs.append(Job("backup", daily_at(offsets["backup"]), backup))
    if restore_check is not None:
        jobs.append(
            Job("backup.restore_check", RESTORE_CHECK_AT, restore_check, RESTORE_CHECK_WEEKDAY)
        )
    return jobs


__all__ = [
    "RESTORE_CHECK_AT",
    "RESTORE_CHECK_WEEKDAY",
    "Job",
    "Scheduler",
    "daily_at",
    "next_fire",
    "nightly_jobs",
]
