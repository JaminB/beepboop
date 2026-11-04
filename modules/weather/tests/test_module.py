"""Unit tests for the `weather` module (docs/MODULES.md §12.4).

`fake_context` is a dry-run ToolContext seeded from ../evals.yaml: one granted
`weather.home` entity, cloudy, 12.5 °C, with a seven-day `forecast` attribute.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from beepboop.module import ToolError
from beepboop.testing import fake_context
from beepboop.testing.evals_model import load_evals

from ..module import Day, Module, current_text, daily, day_name, forecast_text, round_half_up

MODULE_DIR = Path(__file__).resolve().parents[1]
LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 11, 1)


def home_state(**changes: Any) -> list[dict[str, Any]]:
    row = dict(load_evals(MODULE_DIR).fixtures.ha.states[0])
    attrs = dict(row["attributes"])
    attrs.update(changes.pop("attributes", {}))
    row.update(changes, attributes=attrs)
    return [row]


async def test_current_metric() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().current(ctx)
    assert out["speak"] == "It's 13 degrees and cloudy, feels like 10."
    assert out["temperature"] == 12.5
    assert out["wind_speed"] == 14.4
    assert out["units"] == "metric"
    assert "attribution" not in out  # provider free text is never returned
    assert ctx.effects == []


async def test_current_imperial_converts_temperature_and_wind() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice", settings={"units": "imperial"})
    out = await Module().current(ctx)
    assert out["temperature"] == 54.5
    assert out["wind_speed"] == 8.9
    assert out["speak"].startswith("It's 55 degrees")


async def test_fahrenheit_source_to_metric() -> None:
    ha = home_state(
        attributes={"temperature": 50.0, "temperature_unit": "°F", "apparent_temperature": None}
    )
    ctx = fake_context(MODULE_DIR, profile="alice", ha=ha)
    out = await Module().current(ctx)
    assert out["temperature"] == 10.0
    assert out["speak"] == "It's 10 degrees and cloudy."


async def test_offline_entity_is_unavailable() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice", ha=home_state(state="unavailable"))
    with pytest.raises(ToolError) as e:
        await Module().current(ctx)
    assert e.value.code == "unavailable"
    assert "can't reach the internet" in e.value.message
    health = await Module().health(fake_context(MODULE_DIR, ha=home_state(state="unavailable")))
    assert health.status == "degraded"


async def test_no_entity_is_not_found_and_health_degraded() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice", ha=[])
    with pytest.raises(ToolError) as e:
        await Module().current(ctx)
    assert e.value.code == "not_found"
    health = await Module().health(fake_context(MODULE_DIR, ha=[]))
    assert health.status == "degraded"
    assert health.spoken is not None


async def test_unset_entity_setting_falls_back_to_first_weather_entity() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice", settings={"entity": ""})
    assert (await Module().current(ctx))["condition"] == "cloudy"
    assert (await Module().health(fake_context(MODULE_DIR))).status == "ok"


async def test_forecast_tomorrow() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().forecast(ctx, date="2026-11-02", days=1)
    assert out["speak"] == "Tomorrow: rainy, high 11, low 6, 80 percent chance of rain."
    assert out["days"] == [
        {
            "date": "2026-11-02",
            "condition": "rainy",
            "high": 11.0,
            "low": 6.0,
            "precipitation_probability": 80.0,
        }
    ]


async def test_forecast_default_three_days_and_week() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    three = await Module().forecast(ctx)
    assert [d["date"] for d in three["days"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    week = await Module().forecast(ctx, days=7)
    assert week["speak"].endswith("I have 4 more days if you want them.")


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"date": "tomorrow"}, "invalid_args"),
        ({"date": "2026-10-30"}, "invalid_args"),
        ({"date": "2026-11-20"}, "not_found"),
    ],
)
async def test_forecast_bad_dates(kwargs: dict[str, Any], code: str) -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    with pytest.raises(ToolError) as e:
        await Module().forecast(ctx, **kwargs)
    assert e.value.code == code


async def test_forecast_without_attribute_is_unavailable() -> None:
    row = home_state()
    del row[0]["attributes"]["forecast"]
    ctx = fake_context(MODULE_DIR, profile="alice", ha=row)
    with pytest.raises(ToolError) as e:
        await Module().forecast(ctx)
    assert e.value.code == "unavailable"


# ---------------------------------------------------------------- pure helpers


def test_round_half_up() -> None:
    assert [round_half_up(x) for x in (12.5, 13.49, -2.5, 0.0)] == [13, 13, -3, 0]


def test_current_text_skips_close_feels_like() -> None:
    assert current_text("sunny", 20.0, 21.0) == "It's 20 degrees and sunny."
    assert current_text("clear-night", None, None) == "It's clear."


def test_daily_groups_hourly_rows_by_local_date() -> None:
    rows: list[object] = [
        {
            "datetime": "2026-11-02T07:00:00+00:00",
            "condition": "rainy",
            "temperature": 9.0,
        },  # Nov 1 local
        {
            "datetime": "2026-11-02T18:00:00+00:00",
            "condition": "rainy",
            "temperature": 12.0,
            "precipitation_probability": 60,
        },
        {"datetime": "2026-11-02T22:00:00+00:00", "condition": "cloudy", "temperature": 10.0},
        "garbage",
        {"datetime": "not a date"},
    ]
    days = daily(rows, LA, "°C", "metric")
    assert [d.date for d in days] == [date(2026, 11, 1), date(2026, 11, 2)]
    nov2 = days[1]
    assert (nov2.condition, nov2.high, nov2.low, nov2.precipitation_probability) == (
        "rainy",
        12.0,
        10.0,
        60.0,
    )


def test_day_names_and_text() -> None:
    assert day_name(TODAY, TODAY) == "Today"
    assert day_name(date(2026, 11, 6), TODAY) == "Friday"
    assert day_name(date(2026, 11, 9), TODAY) == "November 9"
    same = Day(date(2026, 11, 2), "sunny", 10.0, 10.2, 5.0)
    assert forecast_text([same], TODAY) == "Tomorrow: sunny, high 10."
