"""Presets are grant templates (IDENTITY_AND_TRUST §9), applied once at profile creation.

After that they are just rows. There is no code path per role; ``apply_preset`` only
returns the rows to insert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from beepboop.policy.grants import GROUP_IDS, Grant, ResourceType
from beepboop.policy.types import ProfileKind, Risk

PRESETS_DIR = Path(__file__).with_name("presets")
PRESET_NAMES: tuple[str, ...] = ("admin", "member", "child", "guest")


class PresetGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource: str = Field(pattern=r"^(tool_glob|ha_entity|ha_area|ha_config_entry):.+$")
    max_risk: Risk
    schedule: dict[str, object] | None = None

    @property
    def resource_type(self) -> ResourceType:
        head = self.resource.split(":", 1)[0]
        if head == "tool_glob":
            return "tool_glob"
        if head == "ha_entity":
            return "ha_entity"
        if head == "ha_area":
            return "ha_area"
        return "ha_config_entry"

    @property
    def resource_id(self) -> str:
        return self.resource.split(":", 1)[1]


class Preset(BaseModel):
    """One ``presets/<name>.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProfileKind
    risk_ceiling: Risk
    groups: list[str]
    grants: list[PresetGrant]

    @field_validator("groups")
    @classmethod
    def _known_groups(cls, v: list[str]) -> list[str]:
        unknown = [g for g in v if g not in GROUP_IDS]
        if unknown:
            msg = f"unknown groups {unknown}; only {GROUP_IDS} exist"
            raise ValueError(msg)
        return v


@dataclass(frozen=True, slots=True)
class PresetRows:
    """What applying a preset inserts: the ceiling, group memberships and grant rows."""

    kind: ProfileKind
    risk_ceiling: Risk
    group_ids: tuple[str, ...]
    grants: tuple[Grant, ...]


def parse_preset(text: str) -> Preset:
    return Preset.model_validate(yaml.safe_load(text))


@cache
def load_preset(name: str, directory: Path = PRESETS_DIR) -> Preset:
    if name not in PRESET_NAMES:
        msg = f"unknown preset {name!r}; presets are {PRESET_NAMES}"
        raise KeyError(msg)
    return parse_preset((directory / f"{name}.yaml").read_text(encoding="utf-8"))


def apply_preset(name: str, profile_id: str, new_id: Callable[[], str]) -> PresetRows:
    """Render the preset for ``profile_id``. ``new_id()`` mints each grant id."""
    preset = load_preset(name)
    grants = tuple(
        Grant(
            id=new_id(),
            subject_type="profile",
            subject_id=profile_id,
            resource_type=g.resource_type,
            resource_id=g.resource_id,
            max_risk=g.max_risk,
            source=f"preset:{name}",
        )
        for g in preset.grants
    )
    return PresetRows(preset.kind, preset.risk_ceiling, tuple(preset.groups), grants)
