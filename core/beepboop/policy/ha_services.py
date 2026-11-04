"""HA service classes from ``ha_services.yaml`` (HOME_ASSISTANT §9, IDENTITY_AND_TRUST §6.3)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

import yaml

from beepboop.policy.types import Risk, is_risk, max_tier

ServiceClass = Literal["direct", "indirect", "blocked"]
YAML_PATH = Path(__file__).with_name("ha_services.yaml")


class ServiceTableError(ValueError):
    """``ha_services.yaml`` is malformed."""


@dataclass(frozen=True)
class ServiceTable:
    """Immutable lookup over the YAML. Build with :func:`load_service_table`."""

    direct: dict[str, dict[str, Risk]]
    device_class_floor: dict[str, dict[str, Risk]]
    indirect: frozenset[str]
    blocked: frozenset[str]

    def classify(self, domain: str, service: str | None) -> ServiceClass:
        """``blocked`` wins; then ``indirect`` by domain; ``direct`` if listed; else blocked.

        A pure read (``service is None``, ``home.state``) calls no service, so any domain
        not in ``blocked`` is readable (sensors, binary sensors, weather …).
        """
        if domain in self.blocked:
            return "blocked"
        if domain in self.indirect:
            return "indirect"
        if service is None:
            return "direct"
        services = self.direct.get(domain)
        if services is not None and (service in services or "*" in services):
            return "direct"
        return "blocked"

    def is_blocked(self, domain: str, service: str | None) -> bool:
        return self.classify(domain, service) == "blocked"

    def risk(self, domain: str, service: str | None, device_class: str | None) -> Risk:
        """Tier of a direct service, raised by the device-class floor. Indirect defaults critical.

        Reads (``service is None``) are ``read`` for every readable domain: reading whether
        the garage is closed is not opening it, so device-class floors apply to services only.
        """
        klass = self.classify(domain, service)
        if klass == "blocked":
            msg = f"{domain}.{service or '*'} is blocked"
            raise ServiceTableError(msg)
        if service is None:
            return "read"
        if klass == "indirect":
            return "critical"
        services = self.direct[domain]
        base: Risk = services.get(service, services.get("*", "act"))
        floor = self.device_class_floor.get(domain, {})
        if device_class is not None and device_class in floor:
            return max_tier(base, floor[device_class])
        return base


def _risk_map(raw: object, where: str) -> dict[str, Risk]:
    if not isinstance(raw, dict):
        raise ServiceTableError(f"{where} must be a mapping")
    out: dict[str, Risk] = {}
    items: dict[object, object] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    for k, v in items.items():
        if not isinstance(k, str) or not isinstance(v, str) or not is_risk(v):
            raise ServiceTableError(f"{where}.{k!r}: bad tier {v!r}")
        out[k] = v  # pyright: ignore[reportArgumentType]
    return out


def _str_list(raw: object, where: str) -> frozenset[str]:
    if not isinstance(raw, list):
        raise ServiceTableError(f"{where} must be a list")
    items: list[str] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str):
            raise ServiceTableError(f"{where} entries must be strings")
        items.append(item)
    return frozenset(items)


def parse_service_table(text: str) -> ServiceTable:
    data: object = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ServiceTableError("top level must be a mapping")
    doc: dict[str, object] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    direct_raw = doc.get("direct", {})
    if not isinstance(direct_raw, dict):
        raise ServiceTableError("direct must be a mapping")
    direct = {str(d): _risk_map(s, f"direct.{d}") for d, s in direct_raw.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    floor_raw = doc.get("device_class_floor", {})
    if not isinstance(floor_raw, dict):
        raise ServiceTableError("device_class_floor must be a mapping")
    floor = {str(d): _risk_map(s, f"device_class_floor.{d}") for d, s in floor_raw.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    table = ServiceTable(
        direct=direct,
        device_class_floor=floor,
        indirect=_str_list(doc.get("indirect", []), "indirect"),
        blocked=_str_list(doc.get("blocked", []), "blocked"),
    )
    overlap = (table.indirect & table.blocked) | (
        set(table.direct) & (table.indirect | table.blocked)
    )
    if overlap:
        raise ServiceTableError(f"domains in two classes: {sorted(overlap)}")
    return table


@cache
def load_service_table(path: Path = YAML_PATH) -> ServiceTable:
    """Load and cache the table shipped with core."""
    return parse_service_table(path.read_text(encoding="utf-8"))
