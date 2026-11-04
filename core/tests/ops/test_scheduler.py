"""The nightly scheduler (DATA_AND_PRIVACY §11): local wall-clock times, DST, isolation."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from beepboop.scheduler import Job, Scheduler, next_fire, nightly_jobs

BERLIN = ZoneInfo("Europe/Berlin")


async def _noop(_now: datetime) -> object:
    return None


def test_next_fire_is_local_and_strictly_after() -> None:
    job = Job("backup", time(3, 30), _noop)
    after = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)  # 02:00 in Berlin (CET)
    assert next_fire(job, after, BERLIN) == datetime(2026, 11, 4, 2, 30, tzinfo=UTC)
    at = datetime(2026, 11, 4, 2, 30, tzinfo=UTC)
    assert next_fire(job, at, BERLIN) == datetime(2026, 11, 5, 2, 30, tzinfo=UTC)


def test_next_fire_across_dst_keeps_local_time() -> None:
    job = Job("purge", time(3, 0), _noop)
    # 2027-03-28: Berlin springs forward at 02:00; 03:00 CEST is 01:00 UTC.
    before = datetime(2027, 3, 27, 12, 0, tzinfo=UTC)
    assert next_fire(job, before, BERLIN) == datetime(2027, 3, 28, 1, 0, tzinfo=UTC)


def test_weekly_job_waits_for_its_weekday() -> None:
    job = Job("check", time(4, 0), _noop, weekday=6)
    wednesday = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert next_fire(job, wednesday, UTC) == datetime(2026, 11, 8, 4, 0, tzinfo=UTC)


def test_nightly_jobs_follow_the_retention_offsets() -> None:
    jobs = nightly_jobs(purge=_noop, backup=_noop, restore_check=_noop)
    assert [(j.name, j.at, j.weekday) for j in jobs] == [
        ("retention.purge", time(3, 0), None),
        ("backup", time(3, 30), None),
        ("backup.restore_check", time(4, 0), 6),
    ]
    only_purge = nightly_jobs(purge=_noop, backup=None, restore_check=None)
    assert [j.name for j in only_purge] == ["retention.purge"]


async def test_run_once_sleeps_runs_and_survives_a_failing_job() -> None:
    now = [datetime(2026, 11, 4, 2, 59, tzinfo=UTC)]
    slept: list[float] = []
    ran: list[str] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += timedelta(seconds=seconds)

    async def purge(at: datetime) -> object:
        ran.append(f"purge@{at:%H:%M}")
        raise RuntimeError("disk full")

    async def backup(at: datetime) -> object:
        ran.append(f"backup@{at:%H:%M}")
        return None

    sched = Scheduler(
        nightly_jobs(purge=purge, backup=backup, restore_check=None),
        UTC,
        clock=lambda: now[0],
        sleep=sleep,
    )
    job, outcome = await sched.run_once()
    assert (job.name, outcome) == ("retention.purge", "error")
    job, outcome = await sched.run_once()
    assert (job.name, outcome) == ("backup", "ok")
    assert slept == [pytest.approx(60), pytest.approx(30 * 60)]
    assert ran == ["purge@03:00", "backup@03:30"]
    assert [h[2] for h in sched.history] == ["error", "ok"]


async def test_start_and_stop() -> None:
    sched = Scheduler([Job("x", time(3, 0), _noop)], UTC)
    task = sched.start()
    assert sched.start() is task
    await sched.stop()
    assert task.cancelled()
