"""Tests for the caldav module (docs/MODULES.md §12.4).

Three layers: the handlers against the ``evals.yaml`` fixtures (``fake_context``), the
handlers against an in-memory CalDAV server (``fake_caldav.FakeCalDAV``) for the stateful and
failure paths, and the same server over real HTTP through core's ``HttpxClient``. Pure helpers
are tested directly, with Hypothesis for the iCalendar text round trips.
"""

# Pure helpers are tested directly (MODULES §12.4), so private names are imported on purpose.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.module import ToolError
from beepboop.service_http import HttpxClient
from beepboop.testing import fake_context, run_evals
from beepboop.testing.context import FakeToolContext

from ..module import (
    Event,
    Module,
    _event_from,
    escape,
    event_range,
    fold,
    parse_duration,
    parse_events,
    parse_line,
    parse_value,
    speak_events,
    spoken_day,
    spoken_time,
    to_ics,
    unescape,
    unfold,
    url_allowed,
)
from .fake_caldav import Calendar, FakeCalDAV, FakeCalDAVHttp, serve

MODULE_DIR = Path(__file__).resolve().parents[1]
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=LA)  # evals freeze_time, a Sunday
FIXTURE_ICS = (MODULE_DIR / "tests" / "fixtures" / "calendar.ics").read_text(encoding="utf-8")


def _ctx(**kw: Any) -> FakeToolContext:
    return fake_context(MODULE_DIR, profile="alice", **kw)


def _server() -> FakeCalDAV:
    srv = FakeCalDAV()
    srv.add_calendar("tasks", Calendar("Tasks", comps=("VTODO",)))
    srv.add_calendar("work", Calendar("Work"))
    personal = srv.add_calendar("personal", Calendar("Personal"))
    srv.calendars[personal].events["fixture.ics"] = FIXTURE_ICS
    return srv


def _server_ctx(
    srv: FakeCalDAV,
    *,
    calendar: str = "Personal",
    password: str = "app-password",  # noqa: S107 - the fake server's fixture password
    **kw: Any,
) -> FakeToolContext:
    settings = {"url": kw.pop("url", "https://cal.test/dav/"), "calendar": calendar}
    ctx = _ctx(settings=settings, secrets={"username": "alice", "app_password": password}, **kw)
    return replace(ctx, http=FakeCalDAVHttp(srv))


# ------------------------------------------------------------- the evals, offline --


def test_evals_pass_offline() -> None:
    report = run_evals(MODULE_DIR)
    assert report.ok, [(r.case_id, r.messages) for r in report.failed]


# ---------------------------------------------------- handlers on the evals fixtures --


async def test_events_default_is_rest_of_today() -> None:
    ctx = _ctx()
    out = await Module().events(ctx)
    assert [e["title"] for e in out["events"]] == ["Pick up dry cleaning"]  # cancelled one gone
    assert out["events"][0]["start"] == "2026-11-01T17:00:00-08:00"  # floating → user tz
    assert out["speak"] == "You have 1 event today: Pick up dry cleaning at 5 PM."
    assert ctx.effects == []


async def test_events_tomorrow_reads_tzid_and_utc_times() -> None:
    out = await Module().events(
        _ctx(), start="2026-11-02T00:00:00-08:00", end="2026-11-02T23:59:59-08:00"
    )
    assert [(e["title"], e["start"]) for e in out["events"]] == [
        ("Standup", "2026-11-02T09:30:00-08:00"),
        ("Dentist", "2026-11-02T15:00:00-08:00"),
    ]
    assert out["events"][1]["location"] == "Main St Dental, Suite 4"  # \\, unescaped


async def test_events_all_day_is_a_date() -> None:
    out = await Module().events(_ctx(), start="2026-11-05T00:00:00-08:00")
    assert out["events"] == [
        {
            "title": "Sam's birthday",
            "start": "2026-11-05",
            "end": "2026-11-06",
            "location": None,
            "all_day": True,
        }
    ]
    assert out["speak"] == "You have 1 event on Thursday: Sam's birthday all day."


async def test_query_looks_ahead_and_filters_titles() -> None:
    out = await Module().events(_ctx(), query="DENTIST")
    assert [e["title"] for e in out["events"]] == ["Dentist"]
    out = await Module().events(_ctx(), query="opera")
    assert out["events"] == []
    assert out["speak"].startswith("I couldn't find opera")


async def test_events_never_logs_titles() -> None:
    ctx = _ctx()
    await Module().events(ctx)
    assert "dry cleaning" not in repr(ctx.log_calls).lower()


async def test_events_rejects_inverted_range() -> None:
    with pytest.raises(ToolError) as e:
        await Module().events(_ctx(), start="2026-11-03T00:00:00-08:00", end="2026-11-02T00:00:00")
    assert e.value.code == "invalid_args"


async def test_create_puts_into_the_named_calendar() -> None:
    ctx = _ctx()
    out = await Module().create(ctx, title="Dentist", start="2026-11-06T15:00:00-08:00")
    assert out["speak"] == "Added Dentist on Friday at 3 PM."
    assert out["uid"].endswith("@beepboop")
    put = [e for e in ctx.effects if e["kind"] == "http"]
    assert put == [
        {
            "kind": "http",
            "method": "PUT",
            "url": f"https://cal.test/dav/calendars/alice/personal/{out['uid']}.ics",
        }
    ]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"title": "Dentist", "start": "2026-10-30T15:00:00-08:00"}, "past"),
        (
            {
                "title": "Dentist",
                "start": "2026-11-06T15:00:00-08:00",
                "end": "2026-11-06T14:00:00-08:00",
            },
            "ends before",
        ),
        ({"title": "   ", "start": "2026-11-06T15:00:00-08:00"}, "title"),
        ({"title": "Trip", "start": "2026-10-31T00:00:00-08:00", "all_day": True}, "past"),
        (
            {"title": "Trip", "start": "2026-11-07", "end": "2026-11-06", "all_day": True},
            "ends before",
        ),
        ({"title": "Dentist", "start": "Friday at 3"}, "couldn't read"),
    ],
)
async def test_create_validates_what_the_schema_cannot(
    kwargs: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ToolError) as e:
        await Module().create(_ctx(), **kwargs)
    assert e.value.code == "invalid_args"
    assert fragment in e.value.message


async def test_missing_login_is_reauth_for_tools_and_health() -> None:
    ctx = _ctx(secrets={})
    with pytest.raises(ToolError) as e:
        await Module().events(ctx)
    assert e.value.code == "reauth"
    health = await Module().health(ctx)
    assert health.status == "reauth"
    assert health.spoken == "Your calendar needs reconnecting in the beepboop app."


async def test_public_http_url_is_refused() -> None:
    ctx = _ctx(settings={"url": "http://cal.example.com/dav/"})
    with pytest.raises(ToolError) as e:
        await Module().events(ctx)
    assert e.value.code == "invalid_args"


# --------------------------------------------------- handlers on the fake CalDAV server --


async def test_create_then_read_round_trips_through_the_server() -> None:
    srv = _server()
    mod = Module()
    ctx = _server_ctx(srv)
    out = await mod.create(
        ctx,
        title="Lunch, with Sam; then coffee",
        start="2026-11-03T12:00:00-08:00",
        location="Rosie's",
    )
    personal = srv.calendars[f"{srv.home}personal/"]
    stored = personal.events[f"{out['uid']}.ics"]
    assert "SUMMARY:Lunch\\, with Sam\\; then coffee" in stored
    assert "DTSTART:20261103T200000Z" in stored
    assert stored.endswith("\r\n")
    read = await mod.events(ctx, start="2026-11-03T00:00:00-08:00")
    titles = [e["title"] for e in read["events"]]
    assert titles == ["Lunch, with Sam; then coffee", "Yoga"]
    report = next(r for r in srv.requests if r.method == "REPORT")
    assert report.headers["depth"] == "1"
    assert '<c:time-range start="20261103T080000Z" end="20261104T080000Z"/>' in report.body


async def test_put_uses_if_none_match_and_text_calendar() -> None:
    srv = _server()
    await Module().create(_server_ctx(srv), title="Dentist", start="2026-11-06T15:00:00-08:00")
    put = next(r for r in srv.requests if r.method == "PUT")
    assert put.headers["if-none-match"] == "*"
    assert put.headers["content-type"].startswith("text/calendar")


async def test_discovery_is_cached_per_profile() -> None:
    srv = _server()
    mod = Module()
    ctx = _server_ctx(srv)
    await mod.events(ctx)
    await mod.events(ctx)
    assert [r.method for r in srv.requests].count("PROPFIND") == 3  # root, principal, home
    assert [r.method for r in srv.requests].count("REPORT") == 2


async def test_blank_calendar_picks_the_first_event_calendar() -> None:
    srv = _server()
    srv.calendars[f"{srv.home}work/"].events["w.ics"] = FIXTURE_ICS.replace(
        "Pick up dry cleaning", "Quarterly review"
    )
    out = await Module().events(_server_ctx(srv, calendar=""))
    assert [e["title"] for e in out["events"]] == ["Quarterly review"]  # Work, not Tasks


async def test_calendar_url_is_used_directly() -> None:
    srv = _server()
    url = f"https://cal.test{srv.home}personal/"
    out = await Module().events(_server_ctx(srv, url=url, calendar=""))
    assert [e["title"] for e in out["events"]] == ["Pick up dry cleaning"]
    assert [r.method for r in srv.requests] == ["PROPFIND", "REPORT"]


async def test_unknown_calendar_name_is_not_found_and_degrades_health() -> None:
    srv = _server()
    ctx = _server_ctx(srv, calendar="Holidays")
    with pytest.raises(ToolError) as e:
        await Module().events(ctx)
    assert e.value.code == "not_found"
    assert "Personal" in e.value.message
    health = await Module().health(ctx)
    assert health.status == "degraded"


async def test_wrong_password_is_reauth() -> None:
    srv = _server()
    ctx = _server_ctx(srv, password="wrong")
    with pytest.raises(ToolError) as e:
        await Module().events(ctx)
    assert e.value.code == "reauth"
    assert (await Module().health(ctx)).status == "reauth"


async def test_health_ok_names_the_calendar() -> None:
    health = await Module().health(_server_ctx(_server()))
    assert health.status == "ok"
    assert "Personal" in health.detail


async def test_same_host_redirect_is_followed() -> None:
    srv = _server()
    out = await Module().events(_server_ctx(srv, url="https://cal.test/.well-known/caldav"))
    assert out["events"]


async def test_redirect_to_another_host_is_refused() -> None:
    with pytest.raises(ToolError) as e:
        await Module().events(_server_ctx(_server(), url="https://cal.test/elsewhere/"))
    assert e.value.code == "unavailable"
    assert "full calendar URL" in e.value.message


async def test_moved_calendar_is_rediscovered_once() -> None:
    srv = _server()
    mod = Module()
    ctx = _server_ctx(srv)
    await mod.events(ctx)
    old = f"{srv.home}personal/"
    srv.calendars[f"{srv.home}personal-2/"] = srv.calendars.pop(old)
    out = await mod.events(ctx)
    assert [e["title"] for e in out["events"]] == ["Pick up dry cleaning"]


@pytest.mark.parametrize(
    ("status", "code"), [(500, "unavailable"), (503, "unavailable"), (429, "rate_limited")]
)
async def test_server_errors_map_to_tool_error_codes(status: int, code: str) -> None:
    srv = _server()
    srv.fail_next["PROPFIND"] = status
    with pytest.raises(ToolError) as e:
        await Module().events(_server_ctx(srv))
    assert e.value.code == code


class _GarbageServer(FakeCalDAV):
    def handle(
        self, method: str, url: str, headers: Mapping[str, str], body: str
    ) -> tuple[int, dict[str, str], str]:
        return 207, {}, "<not xml"


async def test_garbage_xml_is_unavailable() -> None:
    srv = _GarbageServer()
    with pytest.raises(ToolError) as e:
        await Module().events(_server_ctx(srv))
    assert e.value.code == "unavailable"


# ------------------------------------------- real HTTP through core's HttpxClient --


async def test_over_real_http_with_the_core_client() -> None:
    srv = _server()
    with serve(srv) as base:
        settings = {"url": f"{base}/dav/", "calendar": "Personal"}
        client = HttpxClient(allowed_hosts=["setting:url"], settings=settings)
        ctx = replace(
            _ctx(settings=settings, secrets={"username": "alice", "app_password": "app-password"}),
            http=client,
        )
        try:
            mod = Module()
            created = await mod.create(ctx, title="Piano", start="2026-11-02T18:00:00-08:00")
            out = await mod.events(ctx, start="2026-11-02T00:00:00-08:00")
        finally:
            await client.aclose()
    assert f"{created['uid']}.ics" in srv.calendars[f"{srv.home}personal/"].events
    assert [e["title"] for e in out["events"]] == ["Standup", "Dentist", "Piano"]


async def test_core_client_refuses_hosts_outside_the_url_setting() -> None:
    client = HttpxClient(allowed_hosts=["setting:url"], settings={"url": "https://cal.test/"})
    try:
        with pytest.raises(ToolError):
            await client.request("PROPFIND", "https://evil.example/dav/", data="<x/>")
    finally:
        await client.aclose()


# --------------------------------------------------------------------- pure helpers --


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://cloud.example.com/remote.php/dav", True),
        ("http://192.168.1.20:5232/", True),
        ("http://127.0.0.1:5232/", True),
        ("http://radicale.local/", True),
        ("http://nas.home.arpa/dav/", True),
        ("http://cal.example.com/", False),
        ("http://8.8.8.8/", False),
        ("ftp://cal.example.com/", False),
        ("not a url", False),
    ],
)
def test_url_allowed(url: str, ok: bool) -> None:
    assert url_allowed(url) is ok


def test_parse_line_handles_quoted_colons() -> None:
    name, params, value = parse_line('DTSTART;TZID="Custom:Zone";X=1:20261102T150000')
    assert (name, params, value) == (
        "DTSTART",
        {"TZID": "Custom:Zone", "X": "1"},
        "20261102T150000",
    )


def test_parse_value_unknown_tzid_falls_back_to_user_tz() -> None:
    dt, is_date = parse_value("20261102T150000", {"TZID": "W. Europe Standard Time"}, LA)
    assert not is_date
    assert dt == datetime(2026, 11, 2, 15, 0, tzinfo=LA)
    dt, is_date = parse_value("20261102", {}, LA)
    assert is_date
    assert dt == datetime(2026, 11, 2, tzinfo=LA)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("PT30M", 30), ("PT1H30M", 90), ("P1D", 1440), ("P1W", 10080), ("-PT15M", -15)],
)
def test_parse_duration(text: str, minutes: int) -> None:
    d = parse_duration(text)
    assert d is not None
    assert d.total_seconds() == minutes * 60


def test_parse_duration_rejects_garbage() -> None:
    assert parse_duration("soon") is None


def test_event_without_start_or_with_bad_date_is_skipped() -> None:
    assert _event_from({"SUMMARY": ({}, "x")}, LA) is None
    assert _event_from({"DTSTART": ({}, "2026-11-02")}, LA) is None


def test_parse_events_ignores_nested_alarms_and_defaults_end() -> None:
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "DTSTART;VALUE=DATE:20261105",
            "SUMMARY:Holiday",
            "BEGIN:VALARM",
            "SUMMARY:Alarm text",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    (ev,) = parse_events(ics, LA)
    assert ev.title == "Holiday"
    assert ev.all_day
    assert (ev.end - ev.start).days == 1


def test_event_range_defaults() -> None:
    s, e = event_range(NOW, LA, None, None, None, 7)
    assert (s, e) == (NOW, datetime(2026, 11, 2, tzinfo=LA))
    s, e = event_range(NOW, LA, None, None, "dentist", 7)
    assert e - s == (datetime(2026, 11, 8, 9, tzinfo=LA) - NOW)
    s, e = event_range(NOW, LA, "2026-11-01T00:00:00", "2026-12-31T00:00:00", None, 7)
    assert (e - s).days == 31  # capped


@pytest.mark.parametrize(
    ("hour", "minute", "spoken"),
    [(15, 0, "3 PM"), (9, 30, "9:30 AM"), (12, 0, "noon"), (0, 0, "midnight"), (0, 5, "12:05 AM")],
)
def test_spoken_time(hour: int, minute: int, spoken: str) -> None:
    assert spoken_time(datetime(2026, 11, 2, hour, minute, tzinfo=LA)) == spoken


@pytest.mark.parametrize(
    ("d", "spoken"),
    [
        (date(2026, 11, 1), "today"),
        (date(2026, 11, 2), "tomorrow"),
        (date(2026, 10, 31), "yesterday"),
        (date(2026, 11, 6), "on Friday"),
        (date(2026, 11, 14), "on November 14"),
    ],
)
def test_spoken_day(d: date, spoken: str) -> None:
    assert spoken_day(d, date(2026, 11, 1)) == spoken


def test_speak_events_names_three_then_counts() -> None:
    evs = [
        Event(
            f"E{i}",
            datetime(2026, 11, 1, 10 + i, tzinfo=LA),
            datetime(2026, 11, 1, 11 + i, tzinfo=LA),
        )
        for i in range(5)
    ]
    end = datetime(2026, 11, 2, tzinfo=LA)
    assert speak_events(evs, NOW, NOW, end, None) == (
        "You have 5 events today: E0 at 10 AM, E1 at 11 AM, E2 at noon, and 2 more."
    )
    assert speak_events([], NOW, NOW, end, None) == "Nothing on your calendar today."


def test_to_ics_folds_long_lines_at_75_octets() -> None:
    ev = Event(
        "é" * 100, datetime(2026, 11, 2, 9, tzinfo=UTC), datetime(2026, 11, 2, 10, tzinfo=UTC)
    )
    ics = to_ics(ev, "u1@beepboop", NOW)
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    (back,) = parse_events(ics, LA)
    assert back.title == "é" * 100


_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"))
    | st.sampled_from(["\n", ";", ",", "\\"]),
    max_size=200,
)


@given(_text)
def test_escape_unescape_round_trip(text: str) -> None:
    assert unescape(escape(text)) == text


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=400))
def test_fold_unfold_round_trip(line: str) -> None:
    folded = fold(f"SUMMARY:{line}")
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert unfold(folded) == [f"SUMMARY:{line}"]
