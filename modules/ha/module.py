"""ha: the five home-control intent tools (docs/MODULES.md §4.7, docs/MCP.md §4.1-§4.5).

Everything goes through ``ctx.ha``, the policy-wrapped client (HOME_ASSISTANT §8): it
resolves only entities the speaker is granted, and ``call_service`` re-checks every call
against policy. This module never imports ``beepboop.ha`` (hard rule 2). Core's target
extractor (``core/beepboop/ha/plan.py``) uses the same tool → service tables as the ones
below to raise the tier before ``authorize``; a core test keeps the two identical.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from beepboop.module import (
    EntityRef,
    EntityState,
    Health,
    ToolContext,
    ToolError,
    ToolResult,
    tool,
)
from beepboop.module import (
    Module as BaseModule,
)

#: ``home.turn_on`` per domain (HOME_ASSISTANT §9).
TURN_ON: Mapping[str, str] = {
    "light": "turn_on",
    "switch": "turn_on",
    "fan": "turn_on",
    "humidifier": "turn_on",
    "media_player": "turn_on",
    "climate": "turn_on",
    "cover": "open_cover",
    "lock": "lock",
    "vacuum": "start",
    "siren": "turn_on",
    "alarm_control_panel": "alarm_arm_away",
    "scene": "turn_on",
    "script": "turn_on",
    "automation": "turn_on",
    "input_boolean": "turn_on",
    "button": "press",
    "input_button": "press",
}
#: ``home.turn_off`` per domain.
TURN_OFF: Mapping[str, str] = {
    "light": "turn_off",
    "switch": "turn_off",
    "fan": "turn_off",
    "humidifier": "turn_off",
    "media_player": "turn_off",
    "climate": "turn_off",
    "cover": "close_cover",
    "lock": "unlock",
    "vacuum": "return_to_base",
    "siren": "turn_off",
    "alarm_control_panel": "alarm_disarm",
    "script": "turn_off",
    "automation": "turn_off",
    "input_boolean": "turn_off",
}
#: ``home.set`` attribute → ``{domain: service}``.
SET: Mapping[str, Mapping[str, str]] = {
    "brightness": {"light": "turn_on"},
    "color": {"light": "turn_on"},
    "color_temp": {"light": "turn_on"},
    "temperature": {"climate": "set_temperature"},
    "hvac_mode": {"climate": "set_hvac_mode"},
    "fan_speed": {"fan": "set_percentage"},
    "position": {"cover": "set_cover_position"},
    "volume": {"media_player": "volume_set"},
    "preset": {"climate": "set_preset_mode", "fan": "set_preset_mode"},
}
#: ``media.control`` action → ``media_player`` service.
MEDIA: Mapping[str, str] = {
    "play": "media_play",
    "pause": "media_pause",
    "stop": "media_stop",
    "next": "media_next_track",
    "previous": "media_previous_track",
    "mute": "volume_mute",
    "unmute": "volume_mute",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "volume_set": "volume_set",
}

#: How the spoken confirmation describes each service.
_DONE_WORD: Mapping[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "open_cover": "opening",
    "close_cover": "closing",
    "lock": "locked",
    "unlock": "unlocked",
    "start": "started",
    "return_to_base": "heading back",
    "press": "pressed",
    "alarm_arm_away": "armed",
    "alarm_disarm": "disarmed",
}
_MEDIA_WORD: Mapping[str, str] = {
    "play": "Playing",
    "pause": "Paused",
    "stop": "Stopped",
    "next": "Skipped",
    "previous": "Back one",
    "mute": "Muted",
    "unmute": "Unmuted",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "volume_set": "Volume set",
}
HVAC_MODES = frozenset({"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"})
#: Spoken-friendly attributes per entity (HOME_ASSISTANT §5). Free text (``media_title``,
#: ``source``) is never returned, so ``home.state`` stays ``untrusted_output: false``.
STATE_ATTRIBUTES = (
    "brightness",
    "color_temp_kelvin",
    "temperature",
    "current_temperature",
    "current_humidity",
    "humidity",
    "hvac_mode",
    "hvac_action",
    "volume_level",
    "is_volume_muted",
    "battery_level",
    "current_position",
    "percentage",
    "unit_of_measurement",
    "device_class",
)
#: ``home.state`` ``attribute`` values (spoken names) → the cache keys they may read, in
#: order of preference. Every key is in ``STATE_ATTRIBUTES``; anything else is never read.
STATE_ATTRIBUTE_KEYS: dict[str, tuple[str, ...]] = {
    "brightness": ("brightness",),
    "color_temp": ("color_temp_kelvin",),
    "temperature": ("temperature", "current_temperature"),
    "current_temperature": ("current_temperature",),
    "humidity": ("current_humidity", "humidity"),
    "hvac_mode": ("hvac_mode",),
    "hvac_action": ("hvac_action",),
    "volume": ("volume_level",),
    "muted": ("is_volume_muted",),
    "battery": ("battery_level",),
    "position": ("current_position",),
    "percentage": ("percentage",),
}
#: An HA ``state`` value is spoken only when it is a number or an enum-like token (``on``,
#: ``heat_cool``, ``2026-11-04T10:00:00+00:00``). Free text in a state (template sensors)
#: is replaced by :data:`UNREADABLE_STATE` so ``home.state`` stays ``untrusted_output: false``.
STATE_TOKEN = re.compile(r"^[A-Za-z0-9_.:+\-]{1,40}$")
UNREADABLE_STATE = "unreadable"
#: The temperature step clamp per call (HOME_ASSISTANT §9).
MAX_TEMPERATURE_STEP = 5.0
#: Child profiles never set a player above this level (HOME_ASSISTANT §9).
CHILD_MAX_VOLUME = 0.7
#: One "louder" step for a child (fraction of full volume), applied through ``volume_set``.
CHILD_VOLUME_STEP = 0.1
SNAPSHOT_MAX_ENTITIES = 40


class _ClarifyError(Exception):
    """The resolver found several distinct devices for a singular name."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ helpers --


def _spoken_target(target: Mapping[str, Any] | None) -> str:
    if not target:
        return "that"
    name = str(target.get("name") or "").strip()
    area = str(target.get("area") or "").strip()
    if name and area:
        return f"{name} in the {area}"
    return name or area or str(target.get("domain") or "that")


def _join(names: Sequence[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _area_label(area_id: str | None) -> str | None:
    return area_id.replace("_", " ").capitalize() if area_id else None


async def _resolve(
    ctx: ToolContext, target: Mapping[str, Any] | None, domains: list[str] | None
) -> list[EntityRef]:
    if not target:
        return []
    name = str(target.get("name") or "").strip()
    if target.get("all") is True:
        name = f"all {name}".strip()
    domain = target.get("domain")
    wanted = [str(domain)] if domain else domains
    area = target.get("area")
    try:
        return await ctx.ha.resolve(name, domains=wanted, area=str(area) if area else None)
    except ToolError as exc:
        if exc.code == "ambiguous":
            raise _ClarifyError(exc.message) from exc
        raise


def _clarify(message: str) -> dict[str, Any]:
    return {"speak": message, "status": "needs_clarification", "affected": []}


def _none(target: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "speak": f"I couldn't find {_spoken_target(target)}.",
        "status": "none_matched",
        "affected": [],
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hex_rgb(value: str) -> list[int] | None:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if m is None:
        return None
    raw = m.group(1)
    return [int(raw[i : i + 2], 16) for i in (0, 2, 4)]


def _is_child(ctx: ToolContext) -> bool:
    return ctx.profile is not None and ctx.profile.kind == "child"


def safe_state(value: str) -> str:
    """The state value ``home.state`` may speak (HOME_ASSISTANT §5): numbers and enum-like
    tokens pass; free text becomes :data:`UNREADABLE_STATE`."""
    return value if STATE_TOKEN.match(value) else UNREADABLE_STATE


def _render_state(st: EntityState) -> str:
    """One spoken clause: "Kitchen ceiling is on at 60%"."""
    name = st.entity.name
    attrs = st.attributes
    domain = st.entity.domain
    st = replace(st, state=safe_state(st.state))
    if domain == "light" and st.state == "on":
        brightness = _number(attrs.get("brightness"))
        if brightness is not None:
            return f"{name} is on at {round(brightness / 255 * 100)}%"
    if domain == "climate":
        current = _number(attrs.get("current_temperature"))
        target = _number(attrs.get("temperature"))
        parts = [f"{name} is {st.state}"]
        if target is not None:
            parts.append(f"set to {target:g}")
        if current is not None:
            parts.append(f"currently {current:g}")
        return ", ".join(parts)
    if domain == "cover":
        position = _number(attrs.get("current_position"))
        if position is not None and st.state == "open" and position < 100:
            return f"{name} is {round(position)}% open"
    if domain in ("sensor", "number"):
        unit = attrs.get("unit_of_measurement")
        suffix = f" {unit}" if isinstance(unit, str) and unit and _unit_ok(unit) else ""
        return f"{name} is {st.state}{suffix}"
    if domain == "binary_sensor":
        return f"{name} is {st.state}"
    return f"{name} is {st.state}"


#: A unit of measurement is at most a few symbols ("°C", "km/h", "%", "W"), never a sentence.
UNIT_TOKEN = re.compile(r"^[^\s]{1,8}$")


def _unit_ok(unit: str) -> bool:
    return UNIT_TOKEN.match(unit) is not None


def _typed(value: object) -> bool:
    """Only numbers, booleans and short enum-like strings leave the cache (MCP §4.4)."""
    if isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and STATE_TOKEN.match(value) is not None


def _entity_row(st: EntityState) -> dict[str, Any]:
    attrs = {
        k: st.attributes[k]
        for k in STATE_ATTRIBUTES
        if k in st.attributes
        and (
            _unit_ok(str(st.attributes[k]))
            if k == "unit_of_measurement"
            else _typed(st.attributes[k])
        )
    }
    return {
        "name": st.entity.name,
        "area": _area_label(st.entity.area_id),
        "state": safe_state(st.state),
        "attributes": attrs,
        "last_changed": st.last_changed,
    }


def default_players(states: Sequence[EntityState], action: str) -> list[EntityRef]:
    """No target: the one granted player that is playing (``paused`` for ``play``), else the
    only granted player (MCP §4.5). Core's extractor applies the same rule."""
    wanted = "paused" if action == "play" else "playing"
    active = [s.entity for s in states if s.state == wanted]
    if len(active) == 1:
        return active
    if not active and len(states) == 1:
        return [states[0].entity]
    return []


# ------------------------------------------------------------------- module --


class Module(BaseModule):
    """Home control. Policy already authorized the tool; ``ctx.ha`` guards each call."""

    async def health(self, ctx: ToolContext) -> Health:
        states = await ctx.ha.states()
        if states and all(s.stale for s in states):
            return Health("degraded", "Home Assistant unreachable", "I can't reach the home.")
        return Health("ok", f"{len(states)} entities")

    # ------------------------------------------------------------ turn on/off --

    @tool("home.turn_on")
    async def turn_on(self, ctx: ToolContext, target: dict[str, Any]) -> dict[str, Any]:
        return await self._switch(ctx, target, TURN_ON)

    @tool("home.turn_off")
    async def turn_off(self, ctx: ToolContext, target: dict[str, Any]) -> dict[str, Any]:
        return await self._switch(ctx, target, TURN_OFF)

    async def _switch(
        self, ctx: ToolContext, target: dict[str, Any], services: Mapping[str, str]
    ) -> dict[str, Any]:
        try:
            entities = await _resolve(ctx, target, None)
        except _ClarifyError as c:
            return _clarify(c.message)
        if not entities:
            return _none(target)
        groups: dict[tuple[str, str], list[EntityRef]] = {}
        skipped: list[str] = []
        for e in entities:
            service = services.get(e.domain)
            if service is None:
                skipped.append(e.name)
                continue
            groups.setdefault((e.domain, service), []).append(e)
        if not groups:
            return {
                "speak": f"I can't switch {_join([e.name for e in entities])}.",
                "status": "none_matched",
                "affected": [],
            }
        affected: list[str] = []
        failed: list[str] = []
        word = ""
        for (domain, service), refs in sorted(groups.items()):
            result = await ctx.ha.call_service(domain, service, refs)
            names = [r.name for r in refs]
            if result.ok:
                affected.extend(names)
                word = _DONE_WORD.get(service, "done")
            else:
                failed.extend(names)
        if not affected:
            return {
                "speak": f"Home Assistant couldn't do that for {_join(failed)}.",
                "status": "partial",
                "affected": [],
            }
        status = "partial" if failed or skipped else "ok"
        verb = "is" if len(affected) == 1 else "are"
        if len(affected) > 3:
            speak = f"{len(affected)} devices {verb} {word}."
        else:
            speak = f"{_join(affected)} {verb} {word}."
        if failed:
            speak += f" {_join(failed)} didn't respond."
        return {"speak": speak, "status": status, "affected": affected}

    # -------------------------------------------------------------------- set --

    @tool("home.set")
    async def set_attribute(
        self,
        ctx: ToolContext,
        target: dict[str, Any],
        attribute: str,
        value: float | str | None = None,
        delta: float | None = None,
    ) -> dict[str, Any]:
        if (value is None) == (delta is None):
            raise ToolError("Give exactly one of value or delta.", code="invalid_args")
        by_domain = SET.get(attribute)
        if by_domain is None:
            raise ToolError(f"Unknown attribute {attribute}.", code="invalid_args")
        try:
            entities = await _resolve(ctx, target, sorted(by_domain))
        except _ClarifyError as c:
            return _clarify(c.message)
        if not entities:
            return _none(target)
        usable = [e for e in entities if e.domain in by_domain]
        if not usable:
            names = _join([e.name for e in entities])
            return {
                "speak": f"I can't change the {attribute.replace('_', ' ')} of {names}.",
                "status": "unsupported",
                "affected": [],
            }
        affected: list[str] = []
        failed: list[str] = []
        new_value: float | str | None = None
        for e in usable:
            current = await ctx.ha.state(e)
            data, shown = self._set_data(ctx, attribute, value, delta, current)
            result = await ctx.ha.call_service(e.domain, by_domain[e.domain], [e], data)
            (affected if result.ok else failed).append(e.name)
            new_value = shown
        if not affected:
            return {
                "speak": f"Home Assistant couldn't do that for {_join(failed)}.",
                "status": "partial",
                "affected": [],
            }
        label = attribute.replace("_", " ")
        shown_text = f"{new_value:g}" if isinstance(new_value, float) else str(new_value)
        if attribute in ("brightness", "position", "volume", "fan_speed"):
            shown_text += "%"
        speak = f"{_join(affected)} {label} set to {shown_text}."
        if failed:
            speak += f" {_join(failed)} didn't respond."
        return {
            "speak": speak,
            "status": "partial" if failed else "ok",
            "affected": affected,
            "new_value": new_value,
        }

    def _set_data(
        self,
        ctx: ToolContext,
        attribute: str,
        value: float | str | None,
        delta: float | None,
        current: EntityState | None,
    ) -> tuple[dict[str, Any], float | str]:
        attrs: Mapping[str, Any] = current.attributes if current is not None else {}
        number = _number(value)
        if attribute in ("color", "hvac_mode", "preset"):
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"Say which {attribute.replace('_', ' ')}.", code="invalid_args")
            text = value.strip().lower()
            if attribute == "hvac_mode":
                mode = text.replace(" ", "_")
                if mode not in HVAC_MODES:
                    raise ToolError(f"{value} is not a thermostat mode.", code="invalid_args")
                return {"hvac_mode": mode}, mode
            if attribute == "preset":
                return {"preset_mode": text}, text
            rgb = _hex_rgb(text)
            return ({"rgb_color": rgb} if rgb else {"color_name": text}), text
        if value is not None and number is None:
            raise ToolError(f"{value} is not a number.", code="invalid_args")

        def absolute(now: float | None, scale: float = 1.0) -> float:
            if number is not None:
                return number
            if now is None:
                raise ToolError(
                    "I don't know the current value; say an exact one.", code="invalid_args"
                )
            return now * scale + float(delta or 0)

        if attribute == "brightness":
            if number is None:
                step = round(float(delta or 0))
                return {"brightness_step_pct": step}, float(step)
            pct = round(_clamp(number, 0, 100))
            return {"brightness_pct": pct}, float(pct)
        if attribute == "color_temp":
            kelvin = round(_clamp(absolute(_number(attrs.get("color_temp_kelvin"))), 1500, 9000))
            return {"color_temp_kelvin": kelvin}, float(kelvin)
        if attribute == "temperature":
            now_target = _number(attrs.get("temperature"))
            wanted = absolute(now_target)
            if now_target is not None:
                wanted = _clamp(
                    wanted, now_target - MAX_TEMPERATURE_STEP, now_target + MAX_TEMPERATURE_STEP
                )
            wanted = round(wanted * 2) / 2
            return {"temperature": wanted}, wanted
        if attribute == "fan_speed":
            pct = round(_clamp(absolute(_number(attrs.get("percentage"))), 0, 100))
            return {"percentage": pct}, float(pct)
        if attribute == "position":
            pct = round(_clamp(absolute(_number(attrs.get("current_position"))), 0, 100))
            return {"position": pct}, float(pct)
        # volume
        level_now = _number(attrs.get("volume_level"))
        pct = _clamp(absolute(level_now, 100.0), 0, 100)
        level = pct / 100
        if _is_child(ctx):
            level = min(level, CHILD_MAX_VOLUME)
        return {"volume_level": round(level, 2)}, float(round(level * 100))

    # ------------------------------------------------------------------ state --

    @tool("home.state")
    async def state(
        self,
        ctx: ToolContext,
        target: dict[str, Any] | None = None,
        attribute: str | None = None,
    ) -> dict[str, Any] | ToolResult:
        if not target:
            return await self._snapshot(ctx)
        try:
            entities = await _resolve(ctx, target, None)
        except _ClarifyError as c:
            return _clarify(c.message) | {"entities": []}
        states = [s for e in entities if (s := await ctx.ha.state(e)) is not None]
        if not states:
            return _none(target) | {"entities": []}
        stale = any(s.stale for s in states)
        clauses = [self._clause(s, attribute) for s in states[:3]]
        speak = "; ".join(clauses) + "."
        if len(states) > 3:
            speak = speak[:-1] + f", and {len(states) - 3} more."
        if stale:
            speak = "I can't reach the home right now; last I knew, " + speak[0].lower() + speak[1:]
        return {
            "speak": speak,
            "status": "ok",
            "stale": stale,
            "entities": [_entity_row(s) for s in states],
        }

    def _clause(self, st: EntityState, attribute: str | None) -> str:
        """Only keys in ``STATE_ATTRIBUTES`` are ever read; ``attribute`` names one of them
        through ``STATE_ATTRIBUTE_KEYS`` (an unknown attribute falls back to the state)."""
        if attribute:
            key = attribute.lower().replace(" ", "_")
            for name in STATE_ATTRIBUTE_KEYS.get(key, ()):
                value = st.attributes.get(name)
                if name in STATE_ATTRIBUTES and value is not None and _typed(value):
                    spoken = str(value).replace("_", " ")
                    return f"{st.entity.name} {attribute.replace('_', ' ')} is {spoken}"
        return _render_state(st)

    async def _snapshot(self, ctx: ToolContext) -> ToolResult:
        """HOME_ASSISTANT §5: grouped by area, granted entities only, capped."""
        states = sorted(
            await ctx.ha.states(),
            key=lambda s: (s.entity.area_id or "~", s.entity.name),
        )[:SNAPSHOT_MAX_ENTITIES]
        lines: dict[str, list[str]] = {}
        for s in states:
            area = _area_label(s.entity.area_id) or "Elsewhere"
            lines.setdefault(area, []).append(_render_state(s).replace(" is ", " ", 1))
        text = "\n".join(f"{area}: {'; '.join(parts)}" for area, parts in lines.items())
        on = sum(1 for s in states if s.entity.domain == "light" and s.state == "on")
        stale = any(s.stale for s in states)
        speak = (
            f"{len(states)} devices; {on} light{'s' if on != 1 else ''} on."
            if states
            else "I can't see any devices for you."
        )
        structured: dict[str, Any] = {
            "speak": speak,
            "status": "ok" if states else "none_matched",
            "stale": stale,
            "entities": [_entity_row(s) for s in states],
        }
        return ToolResult(structured, text=text or speak)

    # ------------------------------------------------------------------ media --

    @tool("media.control")
    async def control(
        self,
        ctx: ToolContext,
        action: str,
        target: dict[str, Any] | None = None,
        volume: int | None = None,
    ) -> dict[str, Any]:
        service = MEDIA.get(action)
        if service is None:
            raise ToolError(f"Unknown action {action}.", code="invalid_args")
        if action == "volume_set" and volume is None:
            raise ToolError("Say a volume from 0 to 100.", code="invalid_args")
        if target:
            try:
                players = await _resolve(ctx, target, ["media_player"])
            except _ClarifyError as c:
                return _clarify(c.message) | {"player": None}
            players = [p for p in players if p.domain == "media_player"]
            if not players:
                return _none(target) | {"player": None}
        else:
            states = await ctx.ha.states(["media_player"])
            players = default_players(states, action)
            if not players:
                if len(states) > 1:
                    names = [s.entity.name for s in states[:3]]
                    return {
                        "speak": f"Which player: {_join(names)}?",
                        "status": "needs_clarification",
                        "player": None,
                        "candidates": names,
                    }
                return {"speak": "Nothing is playing.", "status": "nothing_playing", "player": None}
        data: dict[str, Any] | None = None
        if action in ("mute", "unmute"):
            data = {"is_volume_muted": action == "mute"}
        elif action == "volume_set":
            level = _clamp(float(volume or 0), 0, 100) / 100
            if _is_child(ctx):
                level = min(level, CHILD_MAX_VOLUME)
            data = {"volume_level": round(level, 2)}
        elif action == "volume_up" and _is_child(ctx):
            # HOME_ASSISTANT §9: the child cap holds for steps too, so "louder" becomes a
            # capped volume_set from the current level instead of an uncapped step.
            service = "volume_set"
            data = {"volume_level": await self._child_step_up(ctx, players)}
        result = await ctx.ha.call_service("media_player", service, players, data)
        names = [p.name for p in players]
        if not result.ok:
            return {
                "speak": f"{_join(names)} didn't respond.",
                "status": "none_matched",
                "player": names[0],
            }
        word = _MEDIA_WORD[action]
        speak = f"{word}." if not target else f"{word} on {_join(names)}."
        return {"speak": speak, "status": "ok", "player": names[0]}

    async def _child_step_up(self, ctx: ToolContext, players: Sequence[EntityRef]) -> float:
        """The next ``volume_up`` step for a child: current + 10 %, never above the cap."""
        levels: list[float] = []
        for p in players:
            st = await ctx.ha.state(p)
            level = _number(st.attributes.get("volume_level")) if st is not None else None
            levels.append(level if level is not None else 0.0)
        current = max(levels, default=0.0)
        return round(min(current + CHILD_VOLUME_STEP, CHILD_MAX_VOLUME), 2)
