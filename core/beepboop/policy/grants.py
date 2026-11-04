"""Grants: the one permission primitive (IDENTITY_AND_TRUST §5).

``subject → resource → max_risk → schedule``. A :class:`GrantIndex` is an in-memory,
read-only index rebuilt on any ``grants`` / ``group_members`` / ``ha_entities`` write,
so ``authorize()`` never touches SQL on the hot path (§7).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, tzinfo
from fnmatch import fnmatchcase
from typing import Literal, get_args

from beepboop.policy.types import TIER, EntityInfo, Risk

SubjectType = Literal["profile", "group"]
ResourceType = Literal["tool_glob", "ha_entity", "ha_area", "ha_config_entry"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

WEEKDAYS: tuple[Weekday, ...] = get_args(Weekday)
GROUP_IDS: tuple[str, ...] = ("household", "adults", "children")


class ScheduleError(ValueError):
    """``schedule_json`` is malformed."""


@dataclass(frozen=True, slots=True)
class Schedule:
    """When a grant is active (§5.1). Household time zone; ``start``/``end`` may wrap midnight."""

    days: frozenset[Weekday] | None = None
    start: time | None = None
    end: time | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_json(cls, raw: str | None) -> Schedule | None:
        """Parse ``grants.schedule_json``; ``None`` (or ``"null"``) means always."""
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScheduleError(str(exc)) from exc
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ScheduleError("schedule must be a JSON object")
        obj: dict[str, object] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return cls(
            days=_parse_days(obj.get("days")),
            start=_parse_time(obj.get("start"), "start"),
            end=_parse_time(obj.get("end"), "end"),
            expires_at=_parse_expiry(obj.get("expires_at")),
        )

    def to_json(self) -> str:
        out: dict[str, object] = {}
        if self.days is not None:
            out["days"] = [d for d in WEEKDAYS if d in self.days]
        if self.start is not None:
            out["start"] = self.start.strftime("%H:%M")
        if self.end is not None:
            out["end"] = self.end.strftime("%H:%M")
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at.isoformat()
        return json.dumps(out, sort_keys=True, separators=(",", ":"))

    def is_active(self, now: datetime, tz: tzinfo) -> bool:
        """Evaluate at ``now`` (aware) in the household zone ``tz`` (§5.1)."""
        if now.tzinfo is None:
            msg = "now must be timezone-aware"
            raise ValueError(msg)
        if self.expires_at is not None and now >= self.expires_at:
            return False
        local = now.astimezone(tz)
        if self.days is not None and WEEKDAYS[local.weekday()] not in self.days:
            return False
        if self.start is None and self.end is None:
            return True
        start = self.start or time(0, 0)
        end = self.end or time(23, 59, 59)
        t = local.time().replace(microsecond=0)
        if start <= end:
            return start <= t <= end
        return t >= start or t <= end  # wraps midnight


def schedule_active(schedule: Schedule | None, now: datetime, tz: tzinfo) -> bool:
    """``None`` is always active."""
    return schedule is None or schedule.is_active(now, tz)


@dataclass(frozen=True, slots=True)
class Grant:
    """One ``grants`` row (DATA_AND_PRIVACY §6.1)."""

    id: str
    subject_type: SubjectType
    subject_id: str
    resource_type: ResourceType
    resource_id: str
    max_risk: Risk
    schedule: Schedule | None = None
    source: str = "admin"

    @property
    def subject(self) -> str:
        return f"{self.subject_type}:{self.subject_id}"

    @property
    def resource(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    def covers_tool(self, tool_name: str) -> bool:
        """``tool_glob`` match on the full ``module.verb`` name (``*`` matches everything)."""
        return self.resource_type == "tool_glob" and fnmatchcase(tool_name, self.resource_id)

    def covers_entity(self, entity: EntityInfo) -> bool:
        """§7.2 ``covers_entity``: uuid match, or area / config-entry match on an active entity."""
        if self.resource_type == "ha_entity":
            return entity.uuid == self.resource_id
        if entity.status != "active":
            return False
        if self.resource_type == "ha_area":
            return entity.area_id == self.resource_id
        if self.resource_type == "ha_config_entry":
            return entity.config_entry_id == self.resource_id
        return False

    def strong_enough(self, risk: Risk) -> bool:
        return TIER[self.max_risk] >= TIER[risk]


@dataclass(frozen=True)
class GrantIndex:
    """Grants by subject key (``profile:<id>`` / ``group:<id>``). Immutable; rebuild to change."""

    _by_subject: dict[str, tuple[Grant, ...]] = field(default_factory=dict[str, tuple[Grant, ...]])

    @classmethod
    def build(cls, grants: list[Grant] | tuple[Grant, ...]) -> GrantIndex:
        buckets: defaultdict[str, list[Grant]] = defaultdict(list)
        for g in grants:
            buckets[g.subject].append(g)
        return cls({k: tuple(v) for k, v in buckets.items()})

    def for_subjects(self, profile_id: str, group_ids: frozenset[str]) -> tuple[Grant, ...]:
        """Union of the profile's grants and its groups' grants (§5: union only, no deny rules)."""
        out: list[Grant] = list(self._by_subject.get(f"profile:{profile_id}", ()))
        for gid in sorted(group_ids):
            out.extend(self._by_subject.get(f"group:{gid}", ()))
        return tuple(out)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_subject.values())

    def all(self) -> tuple[Grant, ...]:
        return tuple(g for grants in self._by_subject.values() for g in grants)


def effective_grants(
    index: GrantIndex,
    profile_id: str,
    group_ids: frozenset[str],
    risk: Risk,
    now: datetime,
    tz: tzinfo,
) -> tuple[Grant, ...]:
    """Grants active at ``now`` and strong enough for ``risk`` (§7.2 step 3)."""
    return tuple(
        g
        for g in index.for_subjects(profile_id, group_ids)
        if g.strong_enough(risk) and schedule_active(g.schedule, now, tz)
    )


# --------------------------------------------------------------------- parsing --


def _parse_days(raw: object) -> frozenset[Weekday] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ScheduleError("days must be a list")
    days: set[Weekday] = set()
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str) or item not in WEEKDAYS:
            raise ScheduleError(f"unknown weekday {item!r}")
        days.add(item)
    return frozenset(days)


def _parse_time(raw: object, name: str) -> time | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ScheduleError(f"{name} must be HH:MM")
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError(f"{name} must be HH:MM") from exc
    if parsed.tzinfo is not None:
        raise ScheduleError(f"{name} must not carry a zone")
    return parsed


def _parse_expiry(raw: object) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ScheduleError("expires_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError("expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ScheduleError("expires_at must carry a zone")
    return parsed
