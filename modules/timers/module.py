"""timers: the speaker's own countdown timers, alarms and reminders (docs/MODULES.md §16).

Storage (no core tables, docs/DATA_AND_PRIVACY.md §5):

* ``ctx.kv`` profile scope, key ``timers``: the source of truth, one dict per entry,
  including the label. Deleted with the profile (kv cascade).
* ``ctx.kv.household()``, key ``ring_index``: one *pointer* per pending entry
  (id, profile id, kind, due time, repeat rule). No labels, so nothing a person said
  sits in household scope. ``setup()`` reads it to re-arm after a restart, because a
  hook context has only the household scope (MODULES §5.3).

Ringing: one scheduler task (``ctx.spawn``) sleeps until the next due time, at most
``TICK_S`` at a time, and calls ``ctx.announce(..., profile_id=...)``; the core speaks on
the device that profile last used (MODULES §9). Each tick compares wall-clock and
monotonic progress; when the wall clock steps by more than ``CLOCK_STEP_S`` (NTP sync
after boot, a manual change), duration-based entries are shifted so a 10-minute timer
still rings after ten real minutes, while clock-time alarms stay at their clock time.

Labels are known in memory for entries set in this process. After a restart a pointer
rings with a generic line ("one of your reminders is due"), and the stale profile entry
is pruned the next time that person uses a timer tool.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beepboop.module import Health, KVStore, ToolContext, ToolError, tool
from beepboop.module import Module as BaseModule

Kind = Literal["timer", "alarm", "reminder"]
Repeat = Literal["none", "daily", "weekdays", "weekends"]
Anchor = Literal["duration", "clock"]

KV_TIMERS = "timers"  # profile scope: list[dict]
KV_RING_INDEX = "ring_index"  # household scope: list[dict], no labels
MAX_ACTIVE = 20  # per profile
MAX_AHEAD = timedelta(days=30)
PAST_ROLLOVER = timedelta(hours=24)  # "at" up to a day in the past → next occurrence
LATE_GRACE = timedelta(minutes=5)  # ring if at most this late (e.g. after a restart)
CLOCK_STEP_S = 60.0
TICK_S = 30.0
_KINDS: tuple[Kind, ...] = ("timer", "alarm", "reminder")
_REPEATS: tuple[Repeat, ...] = ("none", "daily", "weekdays", "weekends")
_PLURAL = {"timer": "timers", "alarm": "alarms", "reminder": "reminders"}


# ------------------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class _Entry:
    """One timer, alarm or reminder. ``fires_at`` is aware UTC."""

    id: str
    profile_id: str
    kind: Kind
    fires_at: datetime
    anchor: Anchor
    label: str | None = None
    repeat: Repeat = "none"
    local_time: str | None = None  # "HH:MM" for repeats
    tz: str = "UTC"

    def to_dict(self, *, with_label: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "profile_id": self.profile_id,
            "kind": self.kind,
            "fires_at": self.fires_at.astimezone(UTC).isoformat(),
            "anchor": self.anchor,
            "repeat": self.repeat,
            "local_time": self.local_time,
            "tz": self.tz,
        }
        if with_label:
            d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, profile_id: str | None = None) -> _Entry:
        kind = str(d.get("kind", "timer"))
        repeat = str(d.get("repeat", "none"))
        anchor = str(d.get("anchor", "duration"))
        fires = datetime.fromisoformat(str(d["fires_at"]))
        if fires.tzinfo is None:
            fires = fires.replace(tzinfo=UTC)
        label = d.get("label")
        local_time = d.get("local_time")
        return cls(
            id=str(d["id"]),
            profile_id=str(d.get("profile_id") or profile_id or ""),
            kind=kind if kind in _KINDS else "timer",
            fires_at=fires.astimezone(UTC),
            anchor="clock" if anchor == "clock" else "duration",
            label=str(label) if label else None,
            repeat=repeat if repeat in _REPEATS else "none",
            local_time=str(local_time) if local_time else None,
            tz=str(d.get("tz") or "UTC"),
        )

    def zone(self, fallback: tzinfo) -> tzinfo:
        return _zone(self.tz, fallback)


def _zone(name: str, fallback: tzinfo) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


def _tz_name(tz: tzinfo) -> str:
    key = getattr(tz, "key", None)
    return key if isinstance(key, str) else "UTC"


# -------------------------------------------------------------------- scheduler


class _Scheduler:
    """Rings due entries. Uses only household-scope services, so a hook context works."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep_until_woken: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._wait = sleep_until_woken or _wait_event
        self._wake = asyncio.Event()
        self.labels: dict[str, str | None] = {}  # entry id → label, this process only
        self.profile_kv: dict[str, KVStore] = {}  # profile id → its kv, from a tool call
        self.lock = asyncio.Lock()

    def wake(self) -> None:
        self._wake.set()

    async def run(self, ctx: ToolContext) -> None:
        last_wall, last_mono = ctx.now(), self._monotonic()
        while True:
            now, mono = ctx.now(), self._monotonic()
            step = (now - last_wall).total_seconds() - (mono - last_mono)
            if abs(step) > CLOCK_STEP_S:
                ctx.log.info("timers.clock_step", step_s=round(step))
                await self.shift(ctx, step)
            last_wall, last_mono = now, mono
            nxt = await self.ring_due(ctx, now)
            self._wake.clear()
            delay = TICK_S if nxt is None else min(max((nxt - now).total_seconds(), 0.0), TICK_S)
            await self._wait(self._wake, delay)

    async def ring_due(self, ctx: ToolContext, now: datetime) -> datetime | None:
        """Ring every due pointer; return the next due time, or ``None`` if nothing is pending."""
        async with self.lock:
            index = await _load_index(ctx.kv.household())
            keep: list[_Entry] = []
            fired: list[_Entry] = []
            for p in index:
                if p.fires_at > now:
                    keep.append(p)
                    continue
                fired.append(p)
                if p.repeat != "none" and p.local_time:
                    keep.append(_advance(p, now, ctx.tz))
            if fired:
                await _save_index(ctx.kv.household(), keep)
                for p in fired:
                    await self._update_profile(p, keep)
        for p in fired:
            if now - p.fires_at > LATE_GRACE:
                ctx.log.info(
                    "timers.missed", kind=p.kind, late_s=int((now - p.fires_at).total_seconds())
                )
                continue
            label = self.labels.get(p.id) if p.id in self.labels else _UNKNOWN
            priority = "normal" if p.kind == "reminder" else "high"
            await ctx.announce(
                ring_text(p, label, ctx.tz), profile_id=p.profile_id, priority=priority
            )
            ctx.log.info("timers.rang", kind=p.kind)  # never log the label
            if p.repeat == "none":
                self.labels.pop(p.id, None)
        return min((p.fires_at for p in keep), default=None)

    async def _update_profile(self, fired: _Entry, index: list[_Entry]) -> None:
        kv = self.profile_kv.get(fired.profile_id)
        if kv is None:
            return  # restarted process: pruned lazily on that person's next timer call
        entries = await _load_entries(kv, fired.profile_id)
        nxt = next((p for p in index if p.id == fired.id), None)
        out = [
            replace(e, fires_at=nxt.fires_at) if nxt is not None and e.id == fired.id else e
            for e in entries
            if e.id != fired.id or nxt is not None
        ]
        await _save_entries(kv, out)

    async def shift(self, ctx: ToolContext, step_s: float) -> None:
        """Move duration-anchored entries by a wall-clock step (clock-time ones stay put)."""
        delta = timedelta(seconds=step_s)
        async with self.lock:
            index = await _load_index(ctx.kv.household())
            moved = [
                replace(p, fires_at=p.fires_at + delta) if p.anchor == "duration" else p
                for p in index
            ]
            await _save_index(ctx.kv.household(), moved)
            for pid, kv in self.profile_kv.items():
                entries = await _load_entries(kv, pid)
                await _save_entries(
                    kv,
                    [
                        replace(e, fires_at=e.fires_at + delta) if e.anchor == "duration" else e
                        for e in entries
                    ],
                )


_UNKNOWN = "\x00unknown"  # sentinel: label not known in this process


async def _wait_event(event: asyncio.Event, delay_s: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), delay_s)
    except TimeoutError:
        return


# ----------------------------------------------------------------------- module


class Module(BaseModule):
    """Timers, alarms and reminders. One instance per process."""

    def __init__(self) -> None:
        self._sched = _Scheduler()
        self._task: asyncio.Task[None] | None = None
        self._hook_ctx: ToolContext | None = None

    # ---------------------------------------------------------------- lifecycle

    async def setup(self, ctx: ToolContext) -> None:
        self._hook_ctx = ctx
        index = await _load_index(ctx.kv.household())
        ctx.log.info("timers.setup", pending=len(index))
        if index and not ctx.dry_run:
            self._arm(ctx)

    async def health(self, ctx: ToolContext) -> Health:
        index = await _load_index(ctx.kv.household())
        running = self._task is not None and not self._task.done()
        if index and not running and not ctx.dry_run:
            return Health("degraded", f"{len(index)} pending, scheduler not running")
        return Health("ok", f"{len(index)} pending")

    async def teardown(self, ctx: ToolContext) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # -------------------------------------------------------------------- tools

    @tool("timer.set")
    async def set_timer(
        self,
        ctx: ToolContext,
        kind: Kind,
        duration_s: int | None = None,
        at: str | None = None,
        label: str | None = None,
        repeat: Repeat = "none",
    ) -> dict[str, Any]:
        pid = _profile(ctx)
        now = ctx.now()
        label = (label or "").strip() or None
        entry = _new_entry(pid, kind, duration_s, at, label, repeat, now, ctx.tz)
        async with self._sched.lock:
            entries = _refresh(await _load_entries(ctx.kv, pid), now, ctx.tz)
            if len(entries) >= MAX_ACTIVE:
                raise ToolError(
                    f"The user already has {MAX_ACTIVE} timers, alarms and reminders. "
                    "Ask them to cancel one first.",
                    code="invalid_args",
                )
            entries.append(entry)
            await _save_entries(ctx.kv, entries)
            index = [p for p in await _load_index(ctx.kv.household()) if p.profile_id != pid]
            await _save_index(ctx.kv.household(), index + entries)
        self._remember(ctx, pid, entries)
        ctx.log.info("timers.set", kind=kind, anchor=entry.anchor, repeat=repeat)
        return {
            "speak": set_text(entry, now, ctx.tz),
            "id": entry.id,
            "fires_at": entry.fires_at.astimezone(ctx.tz).isoformat(),
            "kind": kind,
            "label": label,
        }

    @tool("timer.list")
    async def list_timers(
        self, ctx: ToolContext, kind: Literal["timer", "alarm", "reminder", "all"] = "all"
    ) -> dict[str, Any]:
        pid = _profile(ctx)
        now = ctx.now()
        stored = await _load_entries(ctx.kv, pid)
        entries = _refresh(stored, now, ctx.tz)
        if entries != stored:
            async with self._sched.lock:
                await _save_entries(ctx.kv, entries)
        shown = sorted((e for e in entries if kind in ("all", e.kind)), key=lambda e: e.fires_at)
        items = [
            {
                "id": e.id,
                "kind": e.kind,
                "label": e.label,
                "fires_at": e.fires_at.astimezone(ctx.tz).isoformat(),
                "remaining_s": max(0, int((e.fires_at - now).total_seconds())),
                "repeat": e.repeat,
            }
            for e in shown
        ]
        return {"speak": list_text(shown, kind, now, ctx.tz), "items": items}

    @tool("timer.cancel")
    async def cancel(
        self,
        ctx: ToolContext,
        id: str | None = None,  # noqa: A002 - the manifest arg is "id" (docs/MCP.md §4.8)
        label: str | None = None,
        kind: Kind | None = None,
        all: bool = False,  # noqa: A002 - the manifest arg is "all"
    ) -> dict[str, Any]:
        pid = _profile(ctx)
        now = ctx.now()
        async with self._sched.lock:
            entries = _refresh(await _load_entries(ctx.kv, pid), now, ctx.tz)
            pool = [e for e in entries if kind is None or e.kind == kind]
            pool.sort(key=lambda e: e.fires_at)
            if id:
                hits = [e for e in pool if e.id == id]
            elif label:
                hits = match_label(pool, label)
            else:
                hits = pool
            if not hits:
                return {
                    "speak": none_matched_text(label, kind),
                    "cancelled": [],
                    "status": "none_matched",
                }
            if not all and label and len({(h.label or "").lower() for h in hits}) > 1:
                names = _join([describe_short(h) for h in hits[:3]], "or")
                return {
                    "speak": f"Which one: {names}?",
                    "cancelled": [],
                    "status": "needs_clarification",
                }
            chosen = hits if all else hits[:1]
            ids = {e.id for e in chosen}
            await _save_entries(ctx.kv, [e for e in entries if e.id not in ids])
            index = await _load_index(ctx.kv.household())
            await _save_index(ctx.kv.household(), [p for p in index if p.id not in ids])
        for i in ids:
            self._sched.labels.pop(i, None)
        self._sched.wake()
        ctx.log.info("timers.cancel", n=len(chosen))
        return {
            "speak": cancel_text(chosen, now, ctx.tz),
            "cancelled": [e.id for e in chosen],
            "status": "ok",
        }

    # ------------------------------------------------------------------ helpers

    def _remember(self, ctx: ToolContext, pid: str, entries: Iterable[_Entry]) -> None:
        if ctx.dry_run:
            return
        self._sched.profile_kv[pid] = ctx.kv
        for e in entries:
            self._sched.labels[e.id] = e.label
        self._arm(self._hook_ctx or ctx)
        self._sched.wake()

    def _arm(self, ctx: ToolContext) -> None:
        if self._task is None or self._task.done():
            self._task = cast("asyncio.Task[None]", ctx.spawn(self._sched.run(ctx)))


# ------------------------------------------------- pure helpers (unit-tested directly)


def _profile(ctx: ToolContext) -> str:
    if not ctx.profile_id:
        raise ToolError("Timers need to know who is asking.", code="other")
    return ctx.profile_id


def _new_entry(
    pid: str,
    kind: Kind,
    duration_s: int | None,
    at: str | None,
    label: str | None,
    repeat: Repeat,
    now: datetime,
    tz: tzinfo,
) -> _Entry:
    if duration_s is not None and at is not None:
        raise ToolError("Give either a duration or a clock time, not both.", code="invalid_args")
    if kind == "timer" and duration_s is None:
        raise ToolError("Ask the user how long the timer should run.", code="invalid_args")
    if kind == "alarm" and duration_s is None and at is None:
        raise ToolError("Ask the user what time the alarm is for.", code="invalid_args")
    if kind == "reminder":
        if not label:
            raise ToolError("Ask the user what to remind them about.", code="invalid_args")
        if duration_s is None and at is None:
            raise ToolError("Ask the user when to remind them.", code="invalid_args")
    if repeat != "none" and at is None:
        raise ToolError(
            "Only clock-time alarms and reminders can repeat. Ask for a time.", code="invalid_args"
        )
    new_id = uuid.uuid4().hex[:8]
    if at is None:
        seconds = duration_s or 0
        return _Entry(
            new_id, pid, kind, now + timedelta(seconds=seconds), "duration", label, tz=_tz_name(tz)
        )
    fires = resolve_at(at, now, tz)
    local = fires.astimezone(tz)
    if repeat != "none":
        fires = next_occurrence(local.strftime("%H:%M"), tz, now - timedelta(seconds=1), repeat)
    return _Entry(
        new_id,
        pid,
        kind,
        fires,
        "clock",
        label,
        repeat=repeat,
        local_time=local.strftime("%H:%M") if repeat != "none" else None,
        tz=_tz_name(tz),
    )


def resolve_at(at: str, now: datetime, tz: tzinfo) -> datetime:
    """Parse ``at``; a time up to a day in the past rolls forward to its next occurrence."""
    try:
        when = datetime.fromisoformat(at)
    except ValueError as exc:
        raise ToolError(
            "The time must be ISO 8601 with an offset, e.g. 2026-11-02T06:30:00-08:00.",
            code="invalid_args",
        ) from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    when = when.astimezone(UTC)
    if when <= now:
        if now - when > PAST_ROLLOVER:
            raise ToolError(
                "That time has already passed. Ask for a future time.", code="invalid_args"
            )
        when = next_occurrence(when.astimezone(tz).strftime("%H:%M"), tz, now, "daily")
    if when - now > MAX_AHEAD:
        raise ToolError(
            "That is more than 30 days away. Ask for a nearer time.", code="invalid_args"
        )
    return when


def _day_ok(weekday: int, repeat: Repeat) -> bool:
    if repeat == "weekdays":
        return weekday < 5
    if repeat == "weekends":
        return weekday >= 5
    return True


def next_occurrence(local_time: str, tz: tzinfo, after: datetime, repeat: Repeat) -> datetime:
    """The first ``HH:MM`` in ``tz`` strictly after ``after`` on a day ``repeat`` allows (UTC)."""
    hh, mm = (int(x) for x in local_time.split(":", 1))
    start = after.astimezone(tz).date()
    for i in range(9):
        d = start + timedelta(days=i)
        cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        if cand > after and _day_ok(cand.weekday(), repeat):
            return cand.astimezone(UTC)
    msg = "no occurrence within 9 days"  # pragma: no cover - every repeat rule hits in ≤ 3 days
    raise ValueError(msg)


def _advance(p: _Entry, now: datetime, fallback: tzinfo) -> _Entry:
    local_time = p.local_time or say_hhmm(p.fires_at.astimezone(p.zone(fallback)))
    return replace(p, fires_at=next_occurrence(local_time, p.zone(fallback), now, p.repeat))


def say_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _refresh(entries: list[_Entry], now: datetime, tz: tzinfo) -> list[_Entry]:
    """Drop fired one-shots; advance fired repeats (catches up after a restart)."""
    out: list[_Entry] = []
    for e in entries:
        if e.fires_at > now:
            out.append(e)
        elif e.repeat != "none" and e.local_time:
            out.append(_advance(e, now, tz))
    return out


def match_label(entries: list[_Entry], label: str) -> list[_Entry]:
    q = label.lower().strip()
    for word in ("timer", "alarm", "reminder", "the ", "my "):
        q = q.replace(word, " ")
    q = " ".join(q.split())
    if not q:
        return entries
    return [e for e in entries if e.label and (q in e.label.lower() or e.label.lower() in q)]


async def _load_entries(kv: KVStore, pid: str) -> list[_Entry]:
    raw: object = await kv.get(KV_TIMERS, [])
    if not isinstance(raw, list):
        return []
    return [
        _Entry.from_dict(cast(dict[str, Any], d), profile_id=pid)
        for d in cast(list[object], raw)
        if isinstance(d, dict)
    ]


async def _save_entries(kv: KVStore, entries: list[_Entry]) -> None:
    await kv.set(KV_TIMERS, [e.to_dict() for e in entries])


async def _load_index(kv: KVStore) -> list[_Entry]:
    raw: object = await kv.get(KV_RING_INDEX, [])
    if not isinstance(raw, list):
        return []
    return [
        _Entry.from_dict(cast(dict[str, Any], d))
        for d in cast(list[object], raw)
        if isinstance(d, dict)
    ]


async def _save_index(kv: KVStore, entries: list[_Entry]) -> None:
    await kv.set(KV_RING_INDEX, [e.to_dict(with_label=False) for e in entries])


# ------------------------------------------------------------------- speech


def say_duration(seconds: int) -> str:
    """Exact: 90 → "1 minute and 30 seconds"."""
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    parts = [_unit(h, "hour"), _unit(m, "minute"), _unit(s, "second")]
    return _join([p for p in parts if p], "and") or "0 seconds"


def say_remaining(seconds: int) -> str:
    """Rounded for speech: seconds under a minute, else minutes, else hours and minutes."""
    seconds = max(0, seconds)
    if seconds < 60:
        return _unit(seconds, "second") or "less than a second"
    minutes = (seconds + 30) // 60
    if minutes < 60:
        return _unit(minutes, "minute")
    h, m = divmod(minutes, 60)
    return _join([p for p in (_unit(h, "hour"), _unit(m, "minute")) if p], "and")


def say_clock(dt: datetime) -> str:
    """6:30 AM, 3 PM."""
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour} {suffix}" if dt.minute == 0 else f"{hour}:{dt.minute:02d} {suffix}"


def say_when(when: datetime, now: datetime, tz: tzinfo) -> str:
    """ "3 PM", "6:30 AM tomorrow", "3 PM on Friday", "3 PM on November 14"."""
    local = when.astimezone(tz)
    days = (local.date() - now.astimezone(tz).date()).days
    clock = say_clock(local)
    if days <= 0:
        return clock
    if days == 1:
        return f"{clock} tomorrow"
    if days < 7:
        return f"{clock} on {local.strftime('%A')}"
    return f"{clock} on {_month_day(local.date())}"


def _month_day(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}"


_REPEAT_WORDS = {"daily": "every day", "weekdays": "every weekday", "weekends": "every weekend day"}


def set_text(e: _Entry, now: datetime, tz: tzinfo) -> str:
    when = say_when(e.fires_at, now, tz)
    again = f", {_REPEAT_WORDS[e.repeat]}" if e.repeat != "none" else ""
    if e.kind == "timer":
        duration = say_duration(round((e.fires_at - now).total_seconds()))
        return (
            f"{e.label.capitalize()} timer set for {duration}."
            if e.label
            else f"Timer set for {duration}."
        )
    if e.kind == "alarm":
        if e.anchor == "duration":
            return f"Alarm set for {when}."
        return f"Alarm set for {when}{again}."
    if e.anchor == "duration":
        duration = say_duration(round((e.fires_at - now).total_seconds()))
        return f"I'll remind you in {duration}: {e.label}."
    return f"I'll remind you at {when}{again}: {e.label}."


def describe(e: _Entry, now: datetime, tz: tzinfo) -> str:
    if e.kind == "timer":
        rem = say_remaining(int((e.fires_at - now).total_seconds()))
        return f"your {e.label} timer has {rem} left" if e.label else f"your timer has {rem} left"
    again = f" {_REPEAT_WORDS[e.repeat]}" if e.repeat != "none" else ""
    when = say_when(e.fires_at, now, tz)
    if e.kind == "alarm":
        name = f"your {e.label} alarm" if e.label else "your alarm"
        return f"{name} is set for {when}{again}"
    return f"you have a reminder at {when}{again}: {e.label}"


def describe_short(e: _Entry) -> str:
    return f"the {e.label} {e.kind}" if e.label else f"the {e.kind}"


def list_text(entries: list[_Entry], kind: str, now: datetime, tz: tzinfo) -> str:
    if not entries:
        what = "timers, alarms or reminders" if kind == "all" else _PLURAL.get(kind, kind)
        return f"You don't have any {what}."
    parts = [describe(e, now, tz) for e in entries[:3]]
    more = f"; and {len(entries) - 3} more" if len(entries) > 3 else ""
    text = "; ".join(parts) + more + "."
    return text[0].upper() + text[1:]


def cancel_text(chosen: list[_Entry], now: datetime, tz: tzinfo) -> str:
    if len(chosen) == 1:
        e = chosen[0]
        if e.kind == "timer":
            return f"Cancelled your {e.label} timer." if e.label else "Cancelled your timer."
        if e.kind == "alarm":
            return f"Cancelled your alarm for {say_when(e.fires_at, now, tz)}."
        return f"Cancelled your reminder: {e.label}."
    kinds = {e.kind for e in chosen}
    noun = _PLURAL[next(iter(kinds))] if len(kinds) == 1 else "timers, alarms and reminders"
    return f"Cancelled {len(chosen)} {noun}."


def none_matched_text(label: str | None, kind: Kind | None) -> str:
    noun = kind or "timer"
    if label:
        return f"You don't have a {noun} called {label}."
    return (
        "You don't have any timers, alarms or reminders."
        if kind is None
        else f"You don't have any {_PLURAL[kind]}."
    )


def ring_text(p: _Entry, label: str | None, tz: tzinfo) -> str:
    """What the device says when an entry fires. ``label is _UNKNOWN`` after a restart."""
    if label == _UNKNOWN:
        if p.kind == "reminder":
            return "One of your reminders is due."
        if p.kind == "alarm":
            return f"It's {say_clock(p.fires_at.astimezone(p.zone(tz)))}. Your alarm."
        return "Your timer is done."
    if p.kind == "timer":
        return f"Your {label} timer is done." if label else "Your timer is done."
    if p.kind == "alarm":
        clock = say_clock(p.fires_at.astimezone(p.zone(tz)))
        return f"It's {clock}. Your {label} alarm." if label else f"It's {clock}. Your alarm."
    return f"Reminder: {label}." if label else "Reminder."


def _unit(n: int, word: str) -> str:
    if n <= 0:
        return ""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _join(parts: list[str], conj: str) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {conj} " + parts[-1]
