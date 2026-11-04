"""``evals.yaml`` schema (docs/MODULES.md §12.1) as pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from beepboop.policy.grants import ResourceType
from beepboop.policy.types import ProfileKind, Risk

EvalPath = Literal["fast", "llm"]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GrantFixture(_Strict):
    """One grant row in the preset YAML shape (IDENTITY_AND_TRUST §9)."""

    resource: str = Field(pattern=r"^(tool_glob|ha_entity|ha_area|ha_config_entry):.+$")
    max_risk: Risk

    @property
    def resource_type(self) -> ResourceType:
        return cast(ResourceType, self.resource.split(":", 1)[0])

    @property
    def resource_id(self) -> str:
        return self.resource.split(":", 1)[1]


class ProfileFixture(_Strict):
    """A profile for evals. ``grants`` (optional) replaces the preset's grant rows, e.g. a
    child whose parents removed ``timer.*``; ceiling and groups still come from ``kind``."""

    kind: ProfileKind
    locale: str = "en-US"
    tz: str = "UTC"
    name: str | None = None
    grants: list[GrantFixture] | None = None


class HAStates(_Strict):
    states: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class HttpMatch(_Strict):
    method: str = "GET"
    url: str


class HttpResponseFixture(_Strict):
    status: int = 200
    json_: Any = Field(default=None, alias="json")
    text: str | None = None
    text_file: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class HttpFixtureRow(_Strict):
    match: HttpMatch
    response: HttpResponseFixture


class Fixtures(_Strict):
    settings: dict[str, Any] = Field(default_factory=dict[str, Any])
    secrets: dict[str, str] = Field(default_factory=dict[str, str])
    profiles: dict[str, ProfileFixture] = Field(default_factory=dict[str, ProfileFixture])
    kv: dict[str, dict[str, Any]] = Field(default_factory=dict[str, dict[str, Any]])
    ha: HAStates = Field(default_factory=HAStates)
    http: list[HttpFixtureRow] = Field(default_factory=list[HttpFixtureRow])


class Expect(_Strict):
    tool: str | None = None
    args: dict[str, Any] | None = None
    effects: list[dict[str, Any]] | None = None
    speak_contains: list[str] = Field(default_factory=list[str])
    denied: bool = False
    ask_user: bool = False
    confirm: bool = False
    taint: bool = False
    path: EvalPath | None = None


class VoiceFixture(_Strict):
    """The speaker-ID scores of the case's voice principal (§12.1). The default is a
    confidently identified speaker; lower scores exercise the identity step of
    IDENTITY_AND_TRUST §7.2 (an ``act`` call below ``τ_act`` is denied)."""

    confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    margin: float = Field(default=0.30, ge=0.0, le=1.0)


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    utterance: str = Field(min_length=1)
    profile: str = "member"
    voice: VoiceFixture = Field(default_factory=VoiceFixture)
    expect: Expect


class EvalsFile(_Strict):
    api: Literal[1]
    module: str
    freeze_time: str = "2026-11-01T09:00:00-08:00"
    fixtures: Fixtures = Field(default_factory=Fixtures)
    cases: list[EvalCase] = Field(default_factory=list[EvalCase])

    def case(self, case_id: str) -> EvalCase | None:
        return next((c for c in self.cases if c.id == case_id), None)


class EvalsError(ValueError):
    """``evals.yaml`` failed to load or validate (L8)."""


def parse_evals(text: str) -> EvalsFile:
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" line {mark.line + 1}" if mark is not None else ""
        raise EvalsError(f"evals.yaml{where}: not valid YAML") from exc
    if not isinstance(data, dict):
        raise EvalsError("evals.yaml must be a mapping")
    try:
        return EvalsFile.model_validate(data)
    except ValidationError as exc:
        from beepboop.registry.manifest import format_validation_error

        raise EvalsError(format_validation_error(exc, file="evals.yaml")) from exc
    except ValueError as exc:
        raise EvalsError(str(exc)) from exc


def load_evals(module_dir: Path) -> EvalsFile:
    path = module_dir / "evals.yaml"
    if not path.exists():
        raise EvalsError(f"{path} does not exist")
    return parse_evals(path.read_text(encoding="utf-8"))


#: The four default profiles every evals file can name (§12.1).
DEFAULT_PROFILES: dict[str, ProfileFixture] = {
    "admin": ProfileFixture(kind="admin", name="Admin"),
    "member": ProfileFixture(kind="member", name="Member"),
    "child": ProfileFixture(kind="child", name="Child"),
    "guest": ProfileFixture(kind="guest", name="Guest"),
}

__all__ = [
    "DEFAULT_PROFILES",
    "EvalCase",
    "EvalsError",
    "EvalsFile",
    "Expect",
    "Fixtures",
    "GrantFixture",
    "HAStates",
    "HttpFixtureRow",
    "HttpMatch",
    "HttpResponseFixture",
    "ProfileFixture",
    "VoiceFixture",
    "load_evals",
    "parse_evals",
]
