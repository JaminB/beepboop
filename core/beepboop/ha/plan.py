"""Which HA service each ``home.*`` / ``media.*`` call means, and the ``TargetExtractor``
that turns a call into policy ``Target``s before ``authorize`` (HOME_ASSISTANT §6.3, §8, §9).

The registry asks :class:`HATargetExtractor` for targets before it calls
``policy.authorize``; that is how the tier is raised by what the call actually touches
(``lock.unlock`` → ``critical``, a garage cover → ``critical``, a script → its inbox tier)
and how a matched-but-ungranted device becomes a ``no_grant`` denial instead of "I don't
know that device". The ``ha`` module computes the same service names from the same
tables (``modules/ha/module.py``; a core test asserts the two agree) and
``ctx.ha.call_service`` re-checks every call against policy, so a module that drifts is
denied, never trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from beepboop.ha.index import HAEntityIndex
from beepboop.module.types import EntityRef
from beepboop.policy.types import Principal, Target
from beepboop.registry.registry import RegisteredTool
from beepboop.registry.resolver import EntityIndex, Resolution, TargetQuery, resolve

#: ``home.turn_on`` per domain (HOME_ASSISTANT §9.1).
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
#: ``home.turn_off`` per domain. ``lock`` → ``unlock`` and ``alarm_control_panel`` →
#: ``alarm_disarm`` are ``critical`` in ``ha_services.yaml``, so they always need the
#: second factor.
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
#: ``home.set`` ``attribute`` → ``{domain: service}``.
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
#: ``media.control`` ``action`` → ``media_player`` service.
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
PLANNED_TOOLS = frozenset(
    {"home.turn_on", "home.turn_off", "home.set", "home.state", "media.control"}
)


def service_for(tool: str, domain: str, args: Mapping[str, Any]) -> str | None:
    """The HA service ``tool`` calls on an entity of ``domain``; ``None`` for a pure read
    or a domain the tool cannot act on."""
    if tool == "home.turn_on":
        return TURN_ON.get(domain)
    if tool == "home.turn_off":
        return TURN_OFF.get(domain)
    if tool == "home.set":
        return SET.get(str(args.get("attribute", "")), {}).get(domain)
    if tool == "media.control":
        return MEDIA.get(str(args.get("action", ""))) if domain == "media_player" else None
    return None


def query_from_args(tool: str, args: Mapping[str, Any]) -> TargetQuery | None:
    """MCP §4's ``target`` object as a :class:`TargetQuery`; ``None`` when no target was
    given (``home.state`` snapshot, ``media.control`` default player)."""
    raw = args.get("target")
    domain_hint: str | None = None
    if tool == "home.set":
        domains = SET.get(str(args.get("attribute", "")), {})
        domain_hint = next(iter(domains)) if len(domains) == 1 else None
    elif tool == "media.control":
        domain_hint = "media_player"
    if not isinstance(raw, dict):
        return None
    target = cast("dict[str, Any]", raw)
    name = target.get("name")
    area = target.get("area")
    domain = target.get("domain")
    every = target.get("all") is True
    if not name and not area and not domain:
        return None
    return TargetQuery(
        name=str(name) if name else None,
        area=str(area) if area else None,
        domain=str(domain) if domain else domain_hint,
        all=every,
    )


def default_players(index: EntityIndex, principal: Principal, action: str) -> list[EntityRef]:
    """``media.control`` with no target (MCP §4.5): the one granted player that is playing
    (``paused`` for ``play``), else the only granted player, else nothing. The device→area
    default is v0.3 (satellites)."""
    players = [e for e in index.visible(principal) if e.domain == "media_player"]
    wanted = "paused" if action == "play" else "playing"
    active = [p for p in players if index.state_of(p) == wanted]
    if len(active) == 1:
        return active
    if not active and len(players) == 1:
        return players
    return []


def plan_entities(
    tool: str, args: Mapping[str, Any], principal: Principal, index: EntityIndex
) -> tuple[Resolution | None, list[EntityRef]]:
    """The entities a call targets: resolved, or (``denied``) matched but ungranted."""
    query = query_from_args(tool, args)
    if query is None:
        if tool == "media.control":
            return None, default_players(index, principal, str(args.get("action", "")))
        return None, []
    resolution = resolve(principal, query, index)
    if resolution.status == "ok":
        return resolution, list(resolution.targets)
    if resolution.status == "denied":
        return resolution, list(resolution.denied)
    return resolution, []


class HATargetExtractor:
    """``registry.TargetExtractor`` for ``home.*`` and ``media.control``."""

    def __init__(self, index: HAEntityIndex) -> None:
        self._index = index

    def target_for(self, tool: str, args: Mapping[str, Any], ref: EntityRef) -> Target:
        return self.target_with_service(ref, service_for(tool, ref.domain, args))

    def target_with_service(self, ref: EntityRef, service: str | None) -> Target:
        """A policy ``Target``; the inbox tier (``declared_risk``) applies to services only,
        and an undispositioned template switch counts as ``critical`` (§9 indirect)."""
        declared = self._index.entity_info(ref.uuid).declared_risk
        item = self._index.registry.get(ref.uuid)
        if declared is None and item is not None and item.is_template_switch:
            declared = "critical"
        return Target(
            uuid=ref.uuid,
            domain=ref.domain,
            service=service,
            device_class=ref.device_class,
            declared_risk=declared if service is not None else None,
        )

    def targets(self, principal: Principal, tool: str, args: Mapping[str, Any]) -> list[Target]:
        if tool not in PLANNED_TOOLS:
            return []
        _, entities = plan_entities(tool, args, principal, self._index)
        return [self.target_for(tool, args, ref) for ref in entities]

    async def __call__(
        self, principal: Principal, tool: RegisteredTool, args: Mapping[str, Any]
    ) -> list[Target]:
        return self.targets(principal, tool.name, args)


__all__ = [
    "MEDIA",
    "PLANNED_TOOLS",
    "SET",
    "TURN_OFF",
    "TURN_ON",
    "HATargetExtractor",
    "default_players",
    "plan_entities",
    "query_from_args",
    "service_for",
]
