"""weather: current conditions and forecast from a granted HA weather entity (MODULES §5.7).

HA-backed: everything comes from ``ctx.ha`` (granted entities only). No ``ctx.http``, no
secrets, no ``egress``; Home Assistant's weather integration (Met.no by default) holds the
provider connection. Output is numbers and HA's condition enum, never provider free text
(the ``attribution`` attribute is not returned), so ``untrusted_output: false``.

Offline: when the provider cannot be reached, HA marks the entity ``unavailable`` and the
tools raise ``ToolError(code="unavailable")``, which the agent speaks as "I can't reach the
internet to get the weather right now" (ROADMAP v0.1 acceptance 6).

Forecasts: the handler reads the entity's ``forecast`` attribute. Home Assistant 2024.3+
only returns forecasts from the ``weather.get_forecasts`` service response, which the
``HAClient`` contract (MODULES §5.3) does not expose yet; until it does, an entity without
the attribute yields ``unavailable`` (docs/MODULES.md §17).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Literal, cast

from beepboop.module import EntityState, Health, ToolContext, ToolError, tool
from beepboop.module import Module as BaseModule

Units = Literal["metric", "imperial"]
OFFLINE_STATES = frozenset({"unavailable", "unknown"})
SPOKEN_DAYS = 3

CONDITIONS: Mapping[str, str] = {
    "clear-night": "clear",
    "cloudy": "cloudy",
    "exceptional": "unusual",
    "fog": "foggy",
    "hail": "hailing",
    "lightning": "stormy",
    "lightning-rainy": "stormy with rain",
    "partlycloudy": "partly cloudy",
    "pouring": "pouring",
    "rainy": "rainy",
    "snowy": "snowy",
    "snowy-rainy": "sleety",
    "sunny": "sunny",
    "windy": "windy",
    "windy-variant": "windy and cloudy",
}
_WIND_TO_KMH: Mapping[str, float] = {
    "km/h": 1.0,
    "m/s": 3.6,
    "mph": 1.609344,
    "kn": 1.852,
    "ft/s": 1.09728,
}


@dataclass(frozen=True, slots=True)
class Day:
    date: date
    condition: str
    high: float | None
    low: float | None
    precipitation_probability: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "condition": self.condition,
            "high": self.high,
            "low": self.low,
            "precipitation_probability": self.precipitation_probability,
        }


class Module(BaseModule):
    """Two read tools over one HA weather entity."""

    async def health(self, ctx: ToolContext) -> Health:
        try:
            state = await _entity(ctx)
        except ToolError as e:
            return Health(
                "degraded",
                e.message,
                spoken="Weather isn't set up yet. An admin can share a weather source in the "
                "beepboop app.",
            )
        if state.state in OFFLINE_STATES:
            return Health("degraded", f"{state.entity.entity_id} is {state.state}")
        return Health("ok", f"Using {state.entity.entity_id}")

    @tool("weather.current")
    async def current(self, ctx: ToolContext) -> dict[str, Any]:
        state = _online(await _entity(ctx))
        units = _units(ctx)
        attrs = state.attributes
        t_unit = str(attrs.get("temperature_unit") or "°C")
        temp = _temp(_num(attrs.get("temperature")), t_unit, units)
        feels = _temp(_num(attrs.get("apparent_temperature")), t_unit, units)
        wind = _wind(
            _num(attrs.get("wind_speed")), str(attrs.get("wind_speed_unit") or "km/h"), units
        )
        humidity = _num(attrs.get("humidity"))
        condition = state.state
        return {
            "speak": current_text(condition, temp, feels),
            "condition": condition,
            "temperature": temp,
            "feels_like": feels,
            "humidity": humidity,
            "wind_speed": wind,
            "units": units,
        }

    @tool("weather.forecast")
    async def forecast(
        self, ctx: ToolContext, date: str | None = None, days: int = 3
    ) -> dict[str, Any]:
        state = _online(await _entity(ctx))
        units = _units(ctx)
        today = ctx.now().astimezone(ctx.tz).date()
        if date:
            try:
                start = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ToolError("The date must be YYYY-MM-DD.", code="invalid_args") from exc
        else:
            start = today
        if start < today:
            raise ToolError("That day has passed. Ask about today or later.", code="invalid_args")
        raw: object = state.attributes.get("forecast")
        if not isinstance(raw, list) or not raw:
            raise ToolError(
                "Home Assistant isn't providing a forecast for this weather source. "
                "Tell the user only current conditions are available.",
                code="unavailable",
            )
        t_unit = str(state.attributes.get("temperature_unit") or "°C")
        all_days = daily(cast(list[object], raw), ctx.tz, t_unit, units)
        wanted = [d for d in all_days if start <= d.date < start + timedelta(days=days)]
        if not wanted:
            raise ToolError(
                "The forecast doesn't reach that day. Tell the user how far ahead it goes.",
                code="not_found",
            )
        return {
            "speak": forecast_text(wanted, today),
            "days": [d.to_dict() for d in wanted],
            "units": units,
        }


# ------------------------------------------------- pure helpers (unit-tested directly)


async def _entity(ctx: ToolContext) -> EntityState:
    chosen = ctx.settings.get("entity")
    if isinstance(chosen, str) and chosen:
        state = await ctx.ha.state(chosen)
        if state is not None:
            return state
    states = sorted(await ctx.ha.states(["weather"]), key=lambda s: s.entity.entity_id)
    if not states:
        raise ToolError(
            "No weather source is shared with beepboop. "
            "Tell the user an admin can share one in the beepboop app.",
            code="not_found",
        )
    return states[0]


def _online(state: EntityState) -> EntityState:
    if state.state in OFFLINE_STATES:
        raise ToolError(
            "The weather service can't be reached. "
            "Tell the user you can't reach the internet to get the weather right now.",
            code="unavailable",
            retryable=True,
        )
    return state


def _units(ctx: ToolContext) -> Units:
    return "imperial" if ctx.settings.get("units") == "imperial" else "metric"


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def round_half_up(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _temp(value: float | None, unit: str, units: Units) -> float | None:
    if value is None:
        return None
    celsius = (value - 32) * 5 / 9 if "F" in unit.upper() else value
    out = celsius * 9 / 5 + 32 if units == "imperial" else celsius
    return round(out, 1)


def _wind(value: float | None, unit: str, units: Units) -> float | None:
    if value is None:
        return None
    kmh = value * _WIND_TO_KMH.get(unit, 1.0)
    return round(kmh / 1.609344 if units == "imperial" else kmh, 1)


def condition_words(condition: str) -> str:
    return CONDITIONS.get(condition, condition.replace("-", " "))


def current_text(condition: str, temp: float | None, feels: float | None) -> str:
    words = condition_words(condition)
    if temp is None:
        return f"It's {words}."
    text = f"It's {round_half_up(temp)} degrees and {words}"
    if feels is not None and abs(round_half_up(feels) - round_half_up(temp)) >= 2:
        text += f", feels like {round_half_up(feels)}"
    return text + "."


def daily(rows: list[object], tz: tzinfo, t_unit: str, units: Units) -> list[Day]:
    """Group forecast rows (daily or hourly) by local date: max high, min low, max rain chance."""
    by_day: dict[date, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = cast(dict[str, Any], row)
        try:
            when = datetime.fromisoformat(str(r.get("datetime")))
        except ValueError:
            continue
        local = when.astimezone(tz).date() if when.tzinfo else when.date()
        by_day.setdefault(local, []).append(r)
    out: list[Day] = []
    for d in sorted(by_day):
        rows_d = by_day[d]
        highs = [
            v for r in rows_d if (v := _temp(_num(r.get("temperature")), t_unit, units)) is not None
        ]
        lows = [
            v
            for r in rows_d
            if (v := _temp(_num(r.get("templow", r.get("temperature"))), t_unit, units)) is not None
        ]
        rain = [v for r in rows_d if (v := _num(r.get("precipitation_probability"))) is not None]
        conds = Counter(str(r.get("condition")) for r in rows_d if r.get("condition"))
        out.append(
            Day(
                d,
                conds.most_common(1)[0][0] if conds else "unknown",
                max(highs) if highs else None,
                min(lows) if lows else None,
                max(rain) if rain else None,
            )
        )
    return out


def day_name(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 7:
        return d.strftime("%A")
    return f"{d.strftime('%B')} {d.day}"


def day_text(day: Day, today: date) -> str:
    parts = [f"{day_name(day.date, today)}: {condition_words(day.condition)}"]
    if day.high is not None:
        parts.append(f"high {round_half_up(day.high)}")
    if day.low is not None and (
        day.high is None or round_half_up(day.low) != round_half_up(day.high)
    ):
        parts.append(f"low {round_half_up(day.low)}")
    if day.precipitation_probability is not None and day.precipitation_probability >= 20:
        parts.append(f"{round_half_up(day.precipitation_probability)} percent chance of rain")
    return ", ".join(parts) + "."


def forecast_text(days: list[Day], today: date) -> str:
    text = " ".join(day_text(d, today) for d in days[:SPOKEN_DAYS])
    if len(days) > SPOKEN_DAYS:
        text += f" I have {len(days) - SPOKEN_DAYS} more days if you want them."
    return text
