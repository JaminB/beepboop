"""Unit tests for the `timers` module (docs/MODULES.md §12.4).

`fake_context` is a dry-run ToolContext seeded from ../evals.yaml. Scheduler tests swap in a
non-dry-run context with a recording `spawn` and a controllable clock; no real sleeping.
"""

# Pure helpers are tested directly (MODULES §12.4), so private names are imported on purpose.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from beepboop.module import ToolError
from beepboop.testing import fake_context
from beepboop.testing.context import FakeToolContext

from ..module import (
    KV_RING_INDEX,
    KV_TIMERS,
    Module,
    _Entry,
    _refresh,
    _Scheduler,
    match_label,
    next_occurrence,
    resolve_at,
    ring_text,
    say_clock,
    say_duration,
    say_remaining,
    say_when,
)

MODULE_DIR = Path(__file__).resolve().parents[1]
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime.fromisoformat("2026-11-01T09:00:00-08:00")  # Sunday


class _Spawned:
    def __init__(self, coro: Coroutine[Any, Any, Any]) -> None:
        coro.close()
        self.cancelled = False

    def done(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t


def live(
    ctx: FakeToolContext, clock: _Clock | None = None
) -> tuple[FakeToolContext, list[_Spawned]]:
    """The same fakes, but not dry-run, with a recording spawn and an optional clock."""
    spawned: list[_Spawned] = []

    def spawn(coro: Coroutine[Any, Any, Any]) -> _Spawned:
        task = _Spawned(coro)
        spawned.append(task)
        return task

    return replace(ctx, dry_run=False, spawn=spawn, now=clock or ctx.now), spawned


def announcements(ctx: FakeToolContext) -> list[dict[str, Any]]:
    return [e for e in ctx.effects if e["kind"] == "announce"]


# ------------------------------------------------------------------------ timer.set


async def test_set_timer_writes_profile_entry_and_label_free_index() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().set_timer(ctx, kind="timer", duration_s=600, label="eggs")
    assert out["speak"] == "Eggs timer set for 10 minutes."
    assert out["fires_at"] == "2026-11-01T09:10:00-08:00"
    entries = await ctx.kv.get(KV_TIMERS)
    assert len(entries) == 4
    index = await ctx.kv.household().get(KV_RING_INDEX)
    assert {p["profile_id"] for p in index} == {"alice"}
    assert all("label" not in p for p in index)  # nothing a person said in household scope
    assert [e["key"] for e in ctx.effects if e["kind"] == "kv"] == [KV_TIMERS, KV_RING_INDEX]


async def test_set_alarm_in_the_past_rolls_to_tomorrow() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().set_timer(ctx, kind="alarm", at="2026-11-01T06:30:00-08:00")
    assert out["fires_at"] == "2026-11-02T06:30:00-08:00"
    assert out["speak"] == "Alarm set for 6:30 AM tomorrow."


async def test_set_repeating_alarm_skips_to_next_weekday() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"alice": {}})
    out = await Module().set_timer(
        ctx, kind="alarm", at="2026-11-01T07:00:00-08:00", repeat="weekdays"
    )
    assert out["fires_at"] == "2026-11-02T07:00:00-08:00"  # Sunday → Monday
    assert out["speak"].endswith("every weekday.")


async def test_set_reminder_needs_a_message_and_a_time() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    with pytest.raises(ToolError) as e:
        await Module().set_timer(ctx, kind="reminder", duration_s=60)
    assert e.value.code == "invalid_args"
    with pytest.raises(ToolError):
        await Module().set_timer(ctx, kind="reminder", label="call mom")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "timer"},
        {"kind": "alarm"},
        {"kind": "timer", "duration_s": 60, "at": "2026-11-01T10:00:00-08:00"},
        {"kind": "timer", "duration_s": 60, "repeat": "daily"},
        {"kind": "alarm", "at": "not a time"},
        {"kind": "alarm", "at": "2026-10-20T07:00:00-08:00"},
        {"kind": "alarm", "at": "2027-01-20T07:00:00-08:00"},
    ],
)
async def test_set_rejects_bad_args(kwargs: dict[str, Any]) -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    with pytest.raises(ToolError) as e:
        await Module().set_timer(ctx, **kwargs)
    assert e.value.code == "invalid_args"


async def test_set_caps_entries_per_person() -> None:
    entries = [
        _Entry(f"id{i}", "alice", "timer", NOW + timedelta(minutes=i + 1), "duration").to_dict()
        for i in range(20)
    ]
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"alice": {KV_TIMERS: entries}})
    with pytest.raises(ToolError, match="20"):
        await Module().set_timer(ctx, kind="timer", duration_s=60)


async def test_set_needs_a_profile() -> None:
    ctx = fake_context(MODULE_DIR)
    with pytest.raises(ToolError):
        await Module().set_timer(ctx, kind="timer", duration_s=60)


# ----------------------------------------------------------------------- timer.list


async def test_list_reads_all_soonest_first() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().list_timers(ctx)
    assert [i["kind"] for i in out["items"]] == ["timer", "reminder", "alarm"]
    assert out["items"][0]["remaining_s"] == 240
    assert out["speak"].startswith("Your pasta timer has 4 minutes left;")
    assert ctx.effects == []


async def test_list_prunes_fired_entries() -> None:
    later = NOW + timedelta(hours=7)  # pasta and the 3 PM reminder have fired
    ctx = fake_context(MODULE_DIR, profile="alice", now=later)
    out = await Module().list_timers(ctx)
    assert [i["kind"] for i in out["items"]] == ["alarm"]
    assert len(await ctx.kv.get(KV_TIMERS)) == 1


async def test_list_empty_by_kind() -> None:
    ctx = fake_context(MODULE_DIR, profile="member", kv={"member": {}})
    out = await Module().list_timers(ctx, kind="alarm")
    assert out == {"speak": "You don't have any alarms.", "items": []}


# --------------------------------------------------------------------- timer.cancel


async def test_cancel_by_label_removes_entry_and_pointer() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().cancel(ctx, label="the pasta timer")
    assert out["status"] == "ok"
    assert out["cancelled"] == ["a1b2c3d4"]
    assert "a1b2c3d4" not in {e["id"] for e in await ctx.kv.get(KV_TIMERS)}


async def test_cancel_without_args_takes_the_soonest() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().cancel(ctx)
    assert out["speak"] == "Cancelled your pasta timer."


async def test_cancel_ambiguous_label_asks() -> None:
    entries = [
        _Entry("x1", "alice", "timer", NOW + timedelta(minutes=5), "duration", "pasta").to_dict(),
        _Entry(
            "x2", "alice", "timer", NOW + timedelta(minutes=9), "duration", "pasta sauce"
        ).to_dict(),
    ]
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"alice": {KV_TIMERS: entries}})
    out = await Module().cancel(ctx, label="pasta")
    assert out["status"] == "needs_clarification"
    assert out["speak"] == "Which one: the pasta timer or the pasta sauce timer?"


async def test_cancel_all_and_none_matched() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().cancel(ctx, all=True)
    assert out["speak"] == "Cancelled 3 timers, alarms and reminders."
    again = await Module().cancel(ctx, kind="alarm")
    assert again["status"] == "none_matched"
    assert again["speak"] == "You don't have any alarms."


async def test_cancel_by_id() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().cancel(ctx, id="c9d0e1f2")
    assert out["speak"] == "Cancelled your reminder: call mom."


# ------------------------------------------------------------------------ scheduler


async def test_set_arms_the_scheduler_once_outside_dry_run() -> None:
    ctx, spawned = live(fake_context(MODULE_DIR, profile="alice"))
    module = Module()
    await module.set_timer(ctx, kind="timer", duration_s=60)
    await module.set_timer(ctx, kind="timer", duration_s=120)
    assert len(spawned) == 1
    await module.teardown(ctx)
    assert spawned[0].cancelled


async def test_dry_run_never_spawns() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")  # spawn raises in dry-run
    await Module().set_timer(ctx, kind="timer", duration_s=60)


async def test_ring_due_announces_with_label_and_cleans_up() -> None:
    clock = _Clock(NOW)
    ctx, _ = live(fake_context(MODULE_DIR, profile="alice", kv={"alice": {}}), clock)
    module = Module()
    await module.set_timer(ctx, kind="timer", duration_s=60, label="tea")
    clock.t = NOW + timedelta(seconds=61)
    nxt = await module._sched.ring_due(ctx, clock())
    assert nxt is None
    [said] = announcements(ctx)
    assert said == {
        "kind": "announce",
        "text": "Your tea timer is done.",
        "profile_id": "alice",
        "priority": "high",
    }
    assert await ctx.kv.get(KV_TIMERS) == []
    assert await ctx.kv.household().get(KV_RING_INDEX) == []


async def test_repeating_alarm_rings_and_advances() -> None:
    clock = _Clock(NOW)
    ctx, _ = live(fake_context(MODULE_DIR, profile="alice", kv={"alice": {}}), clock)
    module = Module()
    await module.set_timer(ctx, kind="alarm", at="2026-11-02T07:00:00-08:00", repeat="daily")
    clock.t = datetime.fromisoformat("2026-11-02T07:00:30-08:00")
    nxt = await module._sched.ring_due(ctx, clock())
    assert nxt == datetime.fromisoformat("2026-11-03T07:00:00-08:00")
    assert announcements(ctx)[0]["text"] == "It's 7 AM. Your alarm."
    [entry] = await ctx.kv.get(KV_TIMERS)
    assert entry["fires_at"] == "2026-11-03T15:00:00+00:00"


async def test_restart_rings_generically_and_skips_late_ones() -> None:
    index = [
        _Entry("r1", "alice", "reminder", NOW - timedelta(minutes=1), "clock").to_dict(
            with_label=False
        ),
        _Entry("t1", "alice", "timer", NOW - timedelta(hours=2), "duration").to_dict(
            with_label=False
        ),
    ]
    ctx, _ = live(fake_context(MODULE_DIR, kv={"household": {KV_RING_INDEX: index}}))
    sched = _Scheduler()
    assert await sched.ring_due(ctx, NOW) is None
    assert [a["text"] for a in announcements(ctx)] == ["One of your reminders is due."]
    assert announcements(ctx)[0]["priority"] == "normal"


async def test_clock_step_shifts_duration_entries_only() -> None:
    clock = _Clock(NOW)
    ctx, _ = live(fake_context(MODULE_DIR, profile="alice", kv={"alice": {}}), clock)
    module = Module()
    await module.set_timer(ctx, kind="timer", duration_s=600)
    await module.set_timer(ctx, kind="alarm", at="2026-11-02T07:00:00-08:00")
    await module._sched.shift(ctx, 3600.0)
    by_kind = {e["kind"]: e["fires_at"] for e in await ctx.kv.get(KV_TIMERS)}
    assert by_kind == {"timer": "2026-11-01T18:10:00+00:00", "alarm": "2026-11-02T15:00:00+00:00"}


async def test_run_loop_detects_a_clock_step_and_waits_for_the_next_due() -> None:
    clock = _Clock(NOW)
    mono = iter([0.0, 1.0, 2.0])
    waits: list[float] = []

    async def fake_wait(event: asyncio.Event, delay_s: float) -> None:
        waits.append(delay_s)
        if len(waits) == 1:
            clock.t = NOW + timedelta(hours=1, seconds=1)  # wall jumps an hour in 1 s
            return
        raise asyncio.CancelledError

    sched = _Scheduler(monotonic=lambda: next(mono), sleep_until_woken=fake_wait)
    module = Module()
    module._sched = sched
    ctx, _ = live(fake_context(MODULE_DIR, profile="alice", kv={"alice": {}}), clock)
    await module.set_timer(ctx, kind="timer", duration_s=20)
    with pytest.raises(asyncio.CancelledError):
        await sched.run(ctx)
    assert waits[0] == pytest.approx(20.0)
    assert announcements(ctx) == []  # shifted by the step, so not due yet
    [entry] = await ctx.kv.get(KV_TIMERS)
    assert entry["fires_at"] == "2026-11-01T18:00:20+00:00"
    assert waits[1] == pytest.approx(19.0)


async def test_setup_arms_when_pointers_exist_and_health_reports() -> None:
    pointer = _Entry("t1", "alice", "timer", NOW + timedelta(minutes=5), "duration")
    hook, spawned = live(
        fake_context(
            MODULE_DIR, kv={"household": {KV_RING_INDEX: [pointer.to_dict(with_label=False)]}}
        )
    )
    module = Module()
    await module.setup(hook)
    assert len(spawned) == 1
    assert (await module.health(hook)).status == "ok"
    spawned[0].cancel()
    assert (await module.health(hook)).status == "degraded"


# --------------------------------------------------------------------- pure helpers


def test_next_occurrence_respects_repeat_and_dst() -> None:
    after = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # Saturday
    assert next_occurrence("07:00", LA, after, "weekdays").astimezone(LA).weekday() == 0
    assert (
        next_occurrence("07:00", LA, after, "weekends").astimezone(LA).day == 7
    )  # Saturday 07:00 local is still ahead
    # US DST ends 2026-11-01 02:00: 07:00 local is 15:00 UTC on both sides of the change.
    before_change = datetime.fromisoformat("2026-10-31T08:00:00-07:00")
    assert next_occurrence("07:00", LA, before_change, "daily") == datetime(
        2026, 11, 1, 15, tzinfo=UTC
    )


def test_resolve_at_accepts_naive_as_local() -> None:
    when = resolve_at("2026-11-01T10:00:00", NOW.astimezone(UTC), LA)
    assert when == datetime(2026, 11, 1, 18, tzinfo=UTC)


def test_refresh_drops_and_advances() -> None:
    gone = _Entry("a", "p", "timer", NOW - timedelta(seconds=1), "duration")
    daily = _Entry(
        "b",
        "p",
        "alarm",
        NOW - timedelta(hours=1),
        "clock",
        repeat="daily",
        local_time="08:00",
        tz="America/Los_Angeles",
    )
    out = _refresh([gone, daily], NOW, LA)
    assert [e.id for e in out] == ["b"]
    assert out[0].fires_at == datetime.fromisoformat("2026-11-02T08:00:00-08:00")


def test_match_label_ignores_kind_words() -> None:
    e = _Entry("a", "p", "timer", NOW, "duration", "pasta")
    assert match_label([e], "the pasta timer") == [e]
    assert match_label([e], "timer") == [e]
    assert match_label([e], "eggs") == []


def test_spoken_durations_and_times() -> None:
    assert say_duration(90) == "1 minute and 30 seconds"
    assert say_duration(5400) == "1 hour and 30 minutes"
    assert say_duration(3661) == "1 hour, 1 minute and 1 second"
    assert say_remaining(45) == "45 seconds"
    assert say_remaining(239) == "4 minutes"
    assert say_remaining(3900) == "1 hour and 5 minutes"
    assert say_clock(datetime(2026, 1, 1, 0, 5)) == "12:05 AM"
    assert say_clock(datetime(2026, 1, 1, 15, 0)) == "3 PM"
    assert say_when(NOW + timedelta(days=5), NOW, LA) == "9 AM on Friday"
    assert say_when(NOW + timedelta(days=12), NOW, LA) == "9 AM on November 13"


def test_ring_text_by_kind() -> None:
    reminder = _Entry("a", "p", "reminder", NOW, "clock")
    assert ring_text(reminder, "call mom", LA) == "Reminder: call mom."
    timer = _Entry("b", "p", "timer", NOW, "duration")
    assert ring_text(timer, None, LA) == "Your timer is done."
