"""``manifest.yaml`` model and loader (docs/MODULES.md §4).

The pydantic model is the single definition; ``manifest.schema.json`` next to this file is
generated from it (``beepboop module schema`` / ``write_schema``) and must be up to date
(a test checks). Validation here is structural (L1); the prose and budget rules are in
:mod:`beepboop.registry.lint`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from beepboop.policy.types import Risk

MODULE_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
#: ``core`` names the registry built-ins and owns core-only secrets (the HA token); no
#: module may claim it.
RESERVED_MODULE_IDS = frozenset({"core"})
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
#: Registry built-ins (``find_tools``, ``read_guide``) have no namespace.
ANY_TOOL_NAME = re.compile(r"^(?:[a-z][a-z0-9_]*\.)?[a-z][a-z0-9_]*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")

Scope = Literal["household", "profile"]
Category = Literal[
    "home", "media", "productivity", "information", "communication", "persona", "system"
]
SecretKind = Literal["user", "oauth"]

type JsonSchema = dict[str, Any]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _properties(schema: JsonSchema) -> dict[str, Any] | None:
    """``schema["properties"]`` as a typed mapping, or ``None`` if it is not a mapping."""
    props: object = schema.get("properties", {})
    if not isinstance(props, dict):
        return None
    return {str(k): v for k, v in cast(dict[object, Any], props).items()}


class Egress(_Strict):
    destination: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    data: str | None = None
    when: str | None = None


class HARequirement(_Strict):
    domains: list[str] = Field(default_factory=list[str])
    integrations: list[str] = Field(default_factory=list[str])


class Requires(_Strict):
    core: str | None = None
    ha: HARequirement | None = None
    python: list[str] = Field(default_factory=list[str])


class SecretSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str | None = None
    description: str | None = None
    required: bool = True
    kind: SecretKind = "user"


class PromptBlockSpec(_Strict):
    max_tokens: int = Field(ge=1, le=600)


class ToolExample(_Strict):
    utterance: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict[str, Any])


class ToolSpec(_Strict):
    """One ``tools[]`` entry (§4.5)."""

    name: str = Field(pattern=ANY_TOOL_NAME.pattern)
    description: str = Field(min_length=1)
    risk: Risk
    untrusted_output: bool  # required, no default (L17)
    deferred: bool = True
    idempotent: bool = False
    args: JsonSchema
    output: JsonSchema
    examples: list[ToolExample] = Field(default_factory=list[ToolExample])
    timeout_ms: int = Field(default=4000, ge=100, le=60_000)

    @property
    def namespace(self) -> str:
        """``module`` of ``module.verb``; ``"core"`` for a registry built-in."""
        return self.name.split(".", 1)[0] if "." in self.name else "core"

    @property
    def verb(self) -> str:
        return self.name.split(".", 1)[-1]

    @property
    def sensitive_args(self) -> frozenset[str]:
        props = self.args.get("properties", {})
        if not isinstance(props, dict):
            return frozenset()
        out: set[str] = set()
        for key, schema in props.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(schema, dict) and schema.get("x-sensitive") is True:  # pyright: ignore[reportUnknownMemberType]
                out.add(str(key))  # pyright: ignore[reportUnknownArgumentType]
        return frozenset(out)

    @field_validator("args")
    @classmethod
    def _args_shape(cls, v: JsonSchema) -> JsonSchema:
        if v.get("type") != "object":
            msg = "args must be a JSON Schema object (type: object)"
            raise ValueError(msg)
        if v.get("additionalProperties", True) is not False:
            msg = "args must set additionalProperties: false"
            raise ValueError(msg)
        props = _properties(v)
        if props is None:
            msg = "args.properties must be a mapping"
            raise ValueError(msg)
        if len(props) > 8:
            msg = "args may have at most 8 properties"
            raise ValueError(msg)
        return v

    @field_validator("output")
    @classmethod
    def _output_has_speak(cls, v: JsonSchema) -> JsonSchema:
        props = _properties(v)
        if props is None or "speak" not in props:
            msg = "output.properties.speak is required"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _critical_rules(self) -> ToolSpec:
        if self.risk == "critical" and (self.idempotent or self.untrusted_output):
            msg = "critical tools must have idempotent: false and untrusted_output: false (L13)"
            raise ValueError(msg)
        return self


class Manifest(_Strict):
    """``manifest.yaml`` (§4.1)."""

    id: str = Field(pattern=MODULE_ID.pattern)

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_MODULE_IDS:
            msg = f"module id {v!r} is reserved for the registry built-ins (L1)"
            raise ValueError(msg)
        return v

    api: Literal[1]
    version: str = Field(pattern=SEMVER.pattern)
    summary: str = Field(min_length=1)
    #: Human name for the Integrations page ("Calendar"); the id stays in Details (WEB_UI
    #: §11.2). Optional, api:1 additive (§14). Default: the id, capitalised.
    title: str | None = Field(default=None, min_length=1, max_length=40)
    #: What a spoken denial calls the thing the person asked about ("the calendar", never a
    #: tier; VOICE_PIPELINE §11.6). Optional; default: the title or the id in plain words.
    spoken_name: str | None = Field(default=None, min_length=1, max_length=40)
    scope: Scope
    category: Category
    locales: list[str] = Field(default_factory=lambda: ["en"])
    license: str = "GPL-3.0-or-later"
    homepage: str | None = None
    keywords: list[str] = Field(default_factory=list[str], max_length=12)
    requires: Requires | None = None
    egress: list[Egress] = Field(default_factory=list[Egress])
    settings: JsonSchema = Field(default_factory=lambda: {"type": "object", "properties": {}})
    secrets: list[SecretSpec] = Field(default_factory=list[SecretSpec])
    oauth: dict[str, Any] | None = None
    prompt_block: PromptBlockSpec | None = None
    tools: list[ToolSpec] = Field(default_factory=list[ToolSpec])

    @property
    def ha_backed(self) -> bool:
        return self.requires is not None and self.requires.ha is not None

    @property
    def display_title(self) -> str:
        return self.title or self.id.replace("_", " ").capitalize()

    @property
    def display_spoken_name(self) -> str:
        return self.spoken_name or (self.title or self.id).replace("_", " ").lower()

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    @field_validator("secrets", mode="before")
    @classmethod
    def _secret_shorthand(cls, v: object) -> object:
        if isinstance(v, list):
            return [{"name": s} if isinstance(s, str) else s for s in v]  # pyright: ignore[reportUnknownVariableType]
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> Manifest:
        if self.oauth is not None:
            msg = "oauth is v0.2; rejected by the api:1 lint (§4.1)"
            raise ValueError(msg)
        if not self.tools and self.prompt_block is None:
            msg = "tools must have at least one entry unless prompt_block is set"
            raise ValueError(msg)
        if self.prompt_block is not None and self.category != "persona":
            msg = "prompt_block is only allowed for category: persona (L18)"
            raise ValueError(msg)
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            msg = "tool names must be unique"
            raise ValueError(msg)
        bad = [n for n in names if not TOOL_NAME.match(n)]
        if bad:
            msg = f"module tool names must be <namespace>.<verb>: {bad}"
            raise ValueError(msg)
        secret_names = {s.name for s in self.secrets}
        props = self.settings.get("properties", {})
        if isinstance(props, dict):
            clash = secret_names & {str(k) for k in props}  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
            if clash:
                msg = f"secrets names used as settings properties: {sorted(clash)} (L11)"
                raise ValueError(msg)
        return self


class ManifestError(ValueError):
    """``manifest.yaml`` failed to load or validate (L1)."""


def format_validation_error(exc: ValidationError, *, file: str = "manifest.yaml") -> str:
    """One line per problem, ``file path: message``, without pydantic's URL footer
    (``manifest.yaml tools[0]: unknown key 'untrusted_ouput'``)."""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(
            f"[{p}]" if isinstance(p, int) else str(p) for p in err.get("loc", ())
        ).replace(".[", "[")
        kind = err.get("type", "")
        msg = str(err.get("msg", ""))
        if kind == "extra_forbidden":
            key = loc.rsplit(".", 1)[-1].rsplit("[", 1)[-1]
            parent = loc[: -len(key) - 1] if loc.endswith("." + key) else loc
            msg = f"unknown key {key!r}"
            loc = parent
        elif kind == "missing":
            msg = "required"
        lines.append(f"{file} {loc}: {msg}" if loc else f"{file}: {msg}")
    return "; ".join(lines)


def parse_manifest(text: str, *, folder_name: str | None = None) -> Manifest:
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" line {mark.line + 1}" if mark is not None else ""
        raise ManifestError(f"manifest.yaml{where}: not valid YAML ({exc})".split("\n")[0]) from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest.yaml: must be a mapping")
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(format_validation_error(exc)) from exc
    except ValueError as exc:
        raise ManifestError(f"manifest.yaml: {exc}") from exc
    if folder_name is not None and manifest.id != folder_name:
        raise ManifestError(f"manifest id {manifest.id!r} != folder name {folder_name!r} (L1)")
    return manifest


#: The one folder whose name may differ from its ``id``: the worked example ``parcel``.
TEMPLATE_DIR_NAME = "module-template"


def load_manifest(module_dir: Path, *, check_folder: bool = True) -> Manifest:
    """Read and validate ``<module_dir>/manifest.yaml``; the folder name must equal ``id``
    (L1) except for ``docs/module-template``."""
    path = module_dir / "manifest.yaml"
    if not path.exists():
        raise ManifestError(f"{path} does not exist")
    folder = module_dir.name if check_folder and module_dir.name != TEMPLATE_DIR_NAME else None
    return parse_manifest(path.read_text(encoding="utf-8"), folder_name=folder)


def manifest_schema() -> dict[str, Any]:
    """JSON Schema 2020-12 for ``manifest.yaml``, keys sorted deterministically."""
    schema = Manifest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://beepboop.dev/schemas/manifest-api1.json"
    return json.loads(json.dumps(schema, sort_keys=True))


def schema_text() -> str:
    return json.dumps(manifest_schema(), indent=2, sort_keys=True) + "\n"


def write_schema(path: Path = SCHEMA_PATH) -> Path:
    path.write_text(schema_text(), encoding="utf-8", newline="\n")
    return path


__all__ = [
    "MODULE_ID",
    "SCHEMA_PATH",
    "SEMVER",
    "TOOL_NAME",
    "Category",
    "Egress",
    "HARequirement",
    "Manifest",
    "ManifestError",
    "PromptBlockSpec",
    "Requires",
    "Scope",
    "SecretSpec",
    "ToolExample",
    "ToolSpec",
    "format_validation_error",
    "load_manifest",
    "manifest_schema",
    "parse_manifest",
    "schema_text",
    "write_schema",
]
