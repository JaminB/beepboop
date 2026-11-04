"""caldav: read and add events on the person's own CalDAV calendar (docs/MODULES.md §4.8, §16).

Native, ``scope: profile``: every person connects their own server with an app password, so
``ctx.settings`` and ``ctx.secrets`` are that person's. All I/O goes through ``ctx.http``,
which only reaches the host of the ``url`` setting (manifest ``egress: setting:url``).

Protocol (RFC 4791 on top of WebDAV, RFC 4918):

* discovery: ``PROPFIND`` the configured URL for ``resourcetype``, ``current-user-principal``
  and ``calendar-home-set``; follow to the principal and the calendar home; ``PROPFIND``
  ``Depth: 1`` there to list calendars; pick the one named in the ``calendar`` setting or the
  first that holds events. The result is cached per profile in this instance (MODULES §5.4).
* read: ``REPORT`` ``calendar-query`` with a ``time-range`` filter and ``expand`` so the
  server turns recurring events into instances.
* create: ``PUT <calendar>/<uid>.ics`` with ``If-None-Match: *``.

Event text (titles, locations) is written by whoever sent the invitation, so ``caldav.events``
is ``untrusted_output: true``. Titles are personal data: never logged.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beepboop.module import (
    Health,
    HttpMethod,
    HttpResponse,
    ToolContext,
    ToolError,
    tool,
)
from beepboop.module import (
    Module as BaseModule,
)

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CAL}
MAX_EVENTS = 50  # returned to the model; the speak line names at most three
MAX_RANGE = timedelta(days=31)
MAX_REDIRECTS = 3
PAST_GRACE = timedelta(minutes=5)

NOT_CONNECTED = "No calendar connected. Tell the user to connect CalDAV in the beepboop app."
REAUTH = "Calendar login failed. Tell the user to reconnect CalDAV in the beepboop app."
UNAVAILABLE = "The calendar server isn't answering right now. Tell the user to try again later."

PROPFIND_DISCOVER = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:resourcetype/><d:current-user-principal/><c:calendar-home-set/><d:displayname/>"
    "</d:prop></d:propfind>"
)
PROPFIND_CALENDARS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>"
    "</d:prop></d:propfind>"
)


# ----------------------------------------------------------------------------- model --


@dataclass(frozen=True, slots=True)
class Event:
    title: str
    start: datetime  # aware; midnight in the user's timezone for all-day events
    end: datetime
    all_day: bool = False
    location: str | None = None
    uid: str = ""

    def to_output(self, tz: tzinfo) -> dict[str, Any]:
        if self.all_day:
            start = self.start.astimezone(tz).date().isoformat()
            end = self.end.astimezone(tz).date().isoformat()
        else:
            start = self.start.astimezone(tz).isoformat()
            end = self.end.astimezone(tz).isoformat()
        return {
            "title": self.title,
            "start": start,
            "end": end,
            "location": self.location,
            "all_day": self.all_day,
        }


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    href: str
    name: str
    has_events: bool = True


# ------------------------------------------------------------------------- the client --


class CalDAVClient:
    """A minimal CalDAV client over ``ctx.http`` (no third-party CalDAV library)."""

    def __init__(self, ctx: ToolContext, url: str, username: str, password: str) -> None:
        self._ctx = ctx
        self.url = url
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._auth = {"Authorization": f"Basic {token}"}

    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[str, HttpResponse]:
        """Send one request, following same-host redirects. Returns ``(final_url, response)``."""
        host = _host(url)
        for _ in range(MAX_REDIRECTS + 1):
            r = await self._ctx.http.request(
                method,
                url,
                headers={**self._auth, **(headers or {})},
                data=body,
                timeout=_timeout(self._ctx),
            )
            if r.status_code in (301, 302, 307, 308):
                location = _header(r, "location")
                if not location:
                    break
                nxt = urljoin(url, location)
                if _host(nxt) != host:
                    raise ToolError(
                        "The calendar server redirected to another address. Tell the user to "
                        "enter the full calendar URL in the beepboop app.",
                        code="unavailable",
                    )
                url = nxt
                continue
            _raise_for_status(r)
            return url, r
        raise ToolError(UNAVAILABLE, code="unavailable", retryable=True)

    async def propfind(self, url: str, body: str, depth: str) -> tuple[str, ET.Element]:
        final, r = await self.request(
            "PROPFIND",
            url,
            body=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        return final, _parse_xml(r.text)

    async def discover(self, calendar_name: str) -> CalendarInfo:
        """The calendar to use: the URL itself if it is a calendar, else the named one in the
        calendar home, else the first calendar that holds events."""
        base, root = await self.propfind(self.url, PROPFIND_DISCOVER, "0")
        props = _first_props(root)
        if props is not None and _is_calendar(props):
            name = _text(props.find("d:displayname", NS)) or ""
            if not calendar_name or _same_name(name, calendar_name):
                return CalendarInfo(base, name)
        home = _href(props, "c:calendar-home-set", base)
        if home is None:
            principal = _href(props, "d:current-user-principal", base)
            if principal is not None:
                pbase, proot = await self.propfind(principal, PROPFIND_DISCOVER, "0")
                home = _href(_first_props(proot), "c:calendar-home-set", pbase)
        home = home or base
        hbase, hroot = await self.propfind(home, PROPFIND_CALENDARS, "1")
        calendars = list_calendars(hroot, hbase)
        return choose_calendar(calendars, calendar_name)

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> str:
        """The raw iCalendar text of every event overlapping ``[start, end)``."""
        _, r = await self.request(
            "REPORT",
            cal.href,
            body=calendar_query(start, end),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        root = _parse_xml(r.text)
        parts = [el.text or "" for el in root.iter(f"{{{CAL}}}calendar-data")]
        return "\n".join(parts)

    async def put_event(self, cal: CalendarInfo, uid: str, ics: str) -> None:
        url = urljoin(_dir(cal.href), f"{uid}.ics")
        await self.request(
            "PUT",
            url,
            body=ics,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )


# ------------------------------------------------------------------------------ module --


class Module(BaseModule):
    """One instance per process; calendars are cached per profile (MODULES §5.4)."""

    def __init__(self) -> None:
        self._calendars: dict[tuple[str | None, str, str], CalendarInfo] = {}

    async def health(self, ctx: ToolContext) -> Health:
        try:
            client = await _client(ctx)
            cal = await self._calendar(ctx, client, refresh=True)
        except ToolError as e:
            if e.code == "reauth":
                return Health(
                    "reauth",
                    "The CalDAV server rejected the login, or none is saved.",
                    spoken="Your calendar needs reconnecting in the beepboop app.",
                )
            if e.code == "not_found":
                return Health("degraded", e.message)
            return Health("error", f"CalDAV check failed: {e.code}")
        return Health("ok", f"Connected to calendar {cal.name or 'default'}.")

    # ------------------------------------------------------------------- tools --

    @tool("caldav.events")
    async def events(
        self,
        ctx: ToolContext,
        start: str | None = None,
        end: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        now = ctx.now().astimezone(ctx.tz)
        query = (query or "").strip() or None
        range_start, range_end = event_range(
            now, ctx.tz, start, end, query, int(ctx.settings.get("lookahead_days", 7))
        )
        client = await _client(ctx)
        cal = await self._calendar(ctx, client)
        try:
            ics = await client.events(cal, range_start, range_end)
        except ToolError as e:
            if e.code != "not_found":
                raise
            cal = await self._calendar(ctx, client, refresh=True)  # the calendar moved
            ics = await client.events(cal, range_start, range_end)
        found = [
            ev for ev in parse_events(ics, ctx.tz) if ev.start < range_end and ev.end > range_start
        ]
        if query:
            q = query.lower()
            found = [ev for ev in found if q in ev.title.lower()]
        found.sort(key=lambda ev: (ev.start, ev.title))
        found = _dedupe(found)[:MAX_EVENTS]
        ctx.log.info("caldav.events", n=len(found))  # counts only: titles are personal data
        return {
            "speak": speak_events(found, now, range_start, range_end, query),
            "events": [ev.to_output(ctx.tz) for ev in found],
        }

    @tool("caldav.create")
    async def create(
        self,
        ctx: ToolContext,
        title: str,
        start: str,
        end: str | None = None,
        all_day: bool = False,
        location: str | None = None,
    ) -> dict[str, Any]:
        title = " ".join(title.split())
        if not title:
            raise ToolError("The event needs a title. Ask the user what to call it.",
                            code="invalid_args")  # fmt: skip
        now = ctx.now().astimezone(ctx.tz)
        ev = build_event(title, start, end, all_day, (location or "").strip() or None, now, ctx.tz)
        uid = f"{uuid.uuid4()}@beepboop"
        ics = to_ics(ev, uid, now)
        client = await _client(ctx)
        cal = await self._calendar(ctx, client)
        try:
            await client.put_event(cal, uid, ics)
        except ToolError as e:
            if e.code != "not_found":
                raise
            cal = await self._calendar(ctx, client, refresh=True)
            await client.put_event(cal, uid, ics)
        ctx.log.info("caldav.created", uid=uid)  # never the title: it is personal data
        return {"speak": speak_created(ev, now), "uid": uid}

    # ----------------------------------------------------------------- helpers --

    async def _calendar(
        self, ctx: ToolContext, client: CalDAVClient, *, refresh: bool = False
    ) -> CalendarInfo:
        name = str(ctx.settings.get("calendar") or "").strip()
        key = (ctx.profile_id, client.url, name)
        cached = self._calendars.get(key)
        if cached is not None and not refresh:
            return cached
        cal = await client.discover(name)
        self._calendars[key] = cal
        return cal


async def _client(ctx: ToolContext) -> CalDAVClient:
    url = str(ctx.settings.get("url") or "").strip()
    username = await ctx.secrets.get("username")
    password = await ctx.secrets.get("app_password")
    if not url or not username or not password:
        raise ToolError(NOT_CONNECTED, code="reauth")
    if not url_allowed(url):
        raise ToolError(
            "The calendar address must use https. Tell the user to fix the CalDAV URL in the "
            "beepboop app.",
            code="invalid_args",
        )
    return CalDAVClient(ctx, url, username, password)


# ------------------------------------------------------------------ pure helpers: http --


def url_allowed(url: str) -> bool:
    """https anywhere; plain http only to a host on this network (a home Radicale), so an
    app password never crosses the internet in clear text."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if host == "localhost" or host.endswith((".local", ".lan", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _dir(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def _header(r: HttpResponse, name: str) -> str | None:
    for k, v in r.headers.items():
        if k.lower() == name:
            return v
    return None


def _timeout(ctx: ToolContext) -> float | None:
    """Per-request timeout bounded by the handler deadline (``ctx.deadline``, loop time)."""
    if ctx.deadline is None:
        return None
    remaining = ctx.deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise ToolError(UNAVAILABLE, code="timeout", retryable=True)
    return min(5.0, remaining)


def _raise_for_status(r: HttpResponse) -> None:
    code = r.status_code
    if code in (401, 403):
        raise ToolError(REAUTH, code="reauth")
    if code == 404:
        raise ToolError("The calendar was not found on the server.", code="not_found")
    if code == 429:
        raise ToolError(
            "The calendar server is rate limiting us. Tell the user to try again in a minute.",
            code="rate_limited",
            retryable=True,
        )
    if code >= 500:
        raise ToolError(UNAVAILABLE, code="unavailable", retryable=True)
    if code >= 400:
        raise ToolError(f"The calendar server refused the request ({code}).", code="other")


# ------------------------------------------------------------------- pure helpers: xml --


def _parse_xml(text: str) -> ET.Element:
    """Parse a WebDAV response. Stdlib expat (Python 3.12) never fetches external entities
    and caps entity expansion (billion laughs), so ``defusedxml`` is not needed."""
    try:
        return ET.fromstring(text)  # noqa: S314 - expat as above; the host is the user's own server
    except ET.ParseError as exc:
        raise ToolError(
            "The calendar server sent a reply I couldn't read.", code="unavailable"
        ) from exc


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _first_props(root: ET.Element) -> ET.Element | None:
    """The merged ``prop`` of the first ``response`` with status 200."""
    for resp in root.findall("d:response", NS):
        for ps in resp.findall("d:propstat", NS):
            status = _text(ps.find("d:status", NS)) or ""
            if " 200 " in f"{status} ":
                return ps.find("d:prop", NS)
    return None


def _href(props: ET.Element | None, path: str, base: str) -> str | None:
    if props is None:
        return None
    href = _text(props.find(f"{path}/d:href", NS))
    return urljoin(base, href) if href else None


def _is_calendar(props: ET.Element) -> bool:
    return props.find("d:resourcetype/c:calendar", NS) is not None


def _same_name(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def list_calendars(root: ET.Element, base: str) -> list[CalendarInfo]:
    """Every calendar collection in a ``Depth: 1`` multistatus, in server order."""
    out: list[CalendarInfo] = []
    for resp in root.findall("d:response", NS):
        href = _text(resp.find("d:href", NS))
        if not href:
            continue
        for ps in resp.findall("d:propstat", NS):
            status = _text(ps.find("d:status", NS)) or ""
            props = ps.find("d:prop", NS)
            if " 200 " not in f"{status} " or props is None or not _is_calendar(props):
                continue
            comps = props.findall("c:supported-calendar-component-set/c:comp", NS)
            names = {c.get("name", "").upper() for c in comps}
            out.append(
                CalendarInfo(
                    href=urljoin(base, href),
                    name=_text(props.find("d:displayname", NS)) or "",
                    has_events=not names or "VEVENT" in names,
                )
            )
    return out


def choose_calendar(calendars: list[CalendarInfo], name: str) -> CalendarInfo:
    if name:
        for cal in calendars:
            if _same_name(cal.name, name):
                return cal
        known = ", ".join(c.name for c in calendars if c.name)[:120]
        raise ToolError(
            f"There is no calendar called {name}. Tell the user to check the calendar name in "
            f"the beepboop app{f' (found: {known})' if known else ''}.",
            code="not_found",
        )
    for cal in calendars:
        if cal.has_events:
            return cal
    raise ToolError(
        "No calendar for events was found on the server. Tell the user to check the CalDAV "
        "URL in the beepboop app.",
        code="not_found",
    )


def calendar_query(start: datetime, end: datetime) -> str:
    s, e = _utc_stamp(start), _utc_stamp(end)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-data>"
        f'<c:expand start="{s}" end="{e}"/>'
        "</c:calendar-data></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{s}" end="{e}"/>'
        "</c:comp-filter></c:comp-filter></c:filter>"
        "</c:calendar-query>"
    )


# ------------------------------------------------------------- pure helpers: iCalendar --


_DURATION = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def unfold(text: str) -> list[str]:
    """RFC 5545 §3.1: a line starting with a space or tab continues the previous one."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_line(line: str) -> tuple[str, dict[str, str], str]:
    """``NAME;P1=v;P2="a:b":value`` → ``(NAME, {P1: v, P2: a:b}, value)``."""
    in_quote = False
    split_at = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            split_at = i
            break
    if split_at < 0:
        return line.upper(), {}, ""
    head, value = line[:split_at], line[split_at + 1 :]
    parts = head.split(";")
    params: dict[str, str] = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 §3.1: lines longer than 75 octets are split, continuation starts with a space."""
    out: list[str] = []
    current = b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > limit:
            out.append(current.decode("utf-8"))
            current = b""
            limit = 74  # the leading space counts
        current += b
    out.append(current.decode("utf-8"))
    return "\r\n ".join(out)


def parse_value(value: str, params: Mapping[str, str], tz: tzinfo) -> tuple[datetime, bool]:
    """A DTSTART/DTEND value → ``(aware datetime, is_date)``. Floating times and unknown
    ``TZID``s (e.g. Windows zone names) are read in the user's timezone."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(d, time(0), tz), True
    utc = value.endswith("Z")
    naive = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return naive.replace(tzinfo=UTC), False
    zone: tzinfo = tz
    tzid = params.get("TZID")
    if tzid:
        try:
            zone = ZoneInfo(tzid.lstrip("/"))
        except (ZoneInfoNotFoundError, ValueError):
            zone = tz
    return naive.replace(tzinfo=zone), False


def parse_duration(value: str) -> timedelta | None:
    m = _DURATION.match(value.strip())
    if m is None:
        return None
    g = {k: int(v) for k, v in m.groupdict().items() if v and k != "sign"}
    delta = timedelta(
        weeks=g.get("w", 0),
        days=g.get("d", 0),
        hours=g.get("h", 0),
        minutes=g.get("m", 0),
        seconds=g.get("s", 0),
    )
    return -delta if m.group("sign") == "-" else delta


def parse_events(ics: str, tz: tzinfo) -> list[Event]:
    """Every VEVENT in one or more concatenated VCALENDAR texts. Cancelled events and events
    without a start are skipped; a missing end follows RFC 5545 §3.6.1."""
    events: list[Event] = []
    stack: list[str] = []
    props: dict[str, tuple[dict[str, str], str]] = {}
    for line in unfold(ics):
        name, params, value = parse_line(line)
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT":
                props = {}
            continue
        if name == "END":
            if stack and stack[-1] == "VEVENT" and value.upper() == "VEVENT":
                ev = _event_from(props, tz)
                if ev is not None:
                    events.append(ev)
            if stack:
                stack.pop()
            continue
        if stack and stack[-1] == "VEVENT" and name not in props:
            props[name] = (params, value)
    return events


def _event_from(props: Mapping[str, tuple[dict[str, str], str]], tz: tzinfo) -> Event | None:
    if "DTSTART" not in props:
        return None
    if props.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
        return None
    try:
        start, is_date = parse_value(props["DTSTART"][1], props["DTSTART"][0], tz)
        if "DTEND" in props:
            end, _ = parse_value(props["DTEND"][1], props["DTEND"][0], tz)
        elif "DURATION" in props and (d := parse_duration(props["DURATION"][1])) is not None:
            end = start + d
        else:
            end = start + (timedelta(days=1) if is_date else timedelta(0))
    except ValueError:
        return None
    if end < start:
        end = start
    title = unescape(props.get("SUMMARY", ({}, ""))[1]).strip() or "Untitled event"
    location = unescape(props.get("LOCATION", ({}, ""))[1]).strip() or None
    return Event(
        title=title[:200],
        start=start,
        end=end,
        all_day=is_date,
        location=location[:200] if location else None,
        uid=props.get("UID", ({}, ""))[1].strip(),
    )


def _dedupe(events: list[Event]) -> list[Event]:
    """A server that ignores ``expand`` and also returns overrides can repeat an instance."""
    seen: set[tuple[str, datetime, str]] = set()
    out: list[Event] = []
    for ev in events:
        key = (ev.uid, ev.start, ev.title)
        if ev.uid and key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(ev: Event, uid: str, now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//beepboop//caldav module 0.1//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc_stamp(now)}",
    ]
    if ev.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{ev.start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{ev.end.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{_utc_stamp(ev.start)}")
        lines.append(f"DTEND:{_utc_stamp(ev.end)}")
    lines.append(f"SUMMARY:{escape(ev.title)}")
    if ev.location:
        lines.append(f"LOCATION:{escape(ev.location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------- pure helpers: args and time --


def parse_when(value: str, tz: tzinfo) -> datetime:
    """ISO 8601 date-time (or date) → aware datetime; a naive value is in the user's tz."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError(
            "I couldn't read that date or time. Ask the user to say it again.",
            code="invalid_args",
        ) from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def end_of_day(dt: datetime) -> datetime:
    return datetime.combine(dt.date() + timedelta(days=1), time(0), dt.tzinfo)


def event_range(
    now: datetime,
    tz: tzinfo,
    start: str | None,
    end: str | None,
    query: str | None,
    lookahead_days: int,
) -> tuple[datetime, datetime]:
    """The ``[start, end)`` to read (AGENT.md "Arguments"):

    no dates → now to the end of today, or now + ``lookahead_days`` when searching by title;
    start only → to the end of that day; end only → from now. At most 31 days.
    """
    s = parse_when(start, tz).astimezone(tz) if start else now
    if end:
        e = parse_when(end, tz).astimezone(tz)
    elif start:
        e = end_of_day(s)
    elif query:
        e = now + timedelta(days=max(1, min(30, lookahead_days)))
    else:
        e = end_of_day(now)
    if e <= s:
        raise ToolError(
            "The end of that range is before its start. Ask the user which days they mean.",
            code="invalid_args",
        )
    return s, min(e, s + MAX_RANGE)


def build_event(
    title: str,
    start: str,
    end: str | None,
    all_day: bool,
    location: str | None,
    now: datetime,
    tz: tzinfo,
) -> Event:
    """Validate what the schema cannot (MODULES §5.4): past starts, inverted ranges."""
    s = parse_when(start, tz).astimezone(tz)
    if all_day:
        first = s.date()
        last = parse_when(end, tz).astimezone(tz).date() if end else first  # inclusive
        if first < now.date():
            raise ToolError(
                "That day is in the past. Ask the user for a future date.", code="invalid_args"
            )
        if last < first:
            raise ToolError(
                "The event ends before it starts. Ask the user for the dates again.",
                code="invalid_args",
            )
        return Event(
            title,
            datetime.combine(first, time(0), tz),
            datetime.combine(last + timedelta(days=1), time(0), tz),
            all_day=True,
            location=location,
        )
    if s < now - PAST_GRACE:
        raise ToolError(
            "Start time is in the past. Ask the user for a future time.", code="invalid_args"
        )
    e = parse_when(end, tz).astimezone(tz) if end else s + timedelta(hours=1)
    if e <= s:
        raise ToolError(
            "The event ends before it starts. Ask the user for the times again.",
            code="invalid_args",
        )
    return Event(title, s, e, all_day=False, location=location)


# --------------------------------------------------------------- pure helpers: speech --


def spoken_time(dt: datetime) -> str:
    """``15:00`` → "3 PM", ``09:30`` → "9:30 AM", ``12:00`` → "noon"."""
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    h = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{dt.minute:02d} {suffix}" if dt.minute else f"{h} {suffix}"


def spoken_day(d: date, today: date) -> str:
    """ "today", "tomorrow", "on Friday" within a week, else "on November 14"."""
    days = (d - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if 1 < days < 7:
        return f"on {d.strftime('%A')}"
    return f"on {d.strftime('%B')} {d.day}"


def _range_words(start: datetime, end: datetime, today: date) -> str:
    last = (end - timedelta(microseconds=1)).date()
    if start.date() == last:
        return spoken_day(start.date(), today)
    return "in that time"


def _event_words(ev: Event, tz: tzinfo, today: date, with_day: bool) -> str:
    start = ev.start.astimezone(tz)
    if ev.all_day:
        return f"{ev.title} all day" + (f" {spoken_day(start.date(), today)}" if with_day else "")
    day = f" {spoken_day(start.date(), today)}" if with_day else ""
    return f"{ev.title}{day} at {spoken_time(start)}"


def speak_events(
    events: list[Event], now: datetime, start: datetime, end: datetime, query: str | None
) -> str:
    """One sentence; at most three events named, then "and N more" (AGENT.md)."""
    tz = now.tzinfo or UTC
    today = now.date()
    when = _range_words(start, end, today)
    if not events:
        if query:
            return f"I couldn't find {query} on your calendar {when}."
        return f"Nothing on your calendar {when}."
    days = {ev.start.astimezone(tz).date() for ev in events}
    with_day = len(days) > 1 or when == "in that time"
    names = [_event_words(ev, tz, today, with_day) for ev in events[:3]]
    more = len(events) - len(names)
    if more:
        listed = ", ".join(names) + f", and {more} more"
    elif len(names) == 1:
        listed = names[0]
    else:
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
    noun = "event" if len(events) == 1 else "events"
    lead = f"You have {len(events)} {noun}"
    return f"{lead}{'' if with_day else f' {when}'}: {listed}."


def speak_created(ev: Event, now: datetime) -> str:
    tz = now.tzinfo or UTC
    start = ev.start.astimezone(tz)
    day = spoken_day(start.date(), now.date())
    if ev.all_day:
        return f"Added {ev.title} {day}."
    return f"Added {ev.title} {day} at {spoken_time(start)}."
