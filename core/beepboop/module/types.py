"""Value types and Protocols a module sees (docs/MODULES.md §5.1-§5.3).

Everything here is the public SDK surface; ``beepboop.module`` re-exports it. Modules
import nothing else from core (CLAUDE.md hard rule 2), and the lint (L9) checks that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from beepboop.policy.types import Principal, ProfileKind, Risk

ToolErrorCode = Literal[
    "not_found",
    "ambiguous",
    "unavailable",
    "reauth",
    "rate_limited",
    "invalid_args",
    "timeout",
    "other",
]
HealthStatus = Literal["ok", "degraded", "reauth", "error"]
AnnouncePriority = Literal["low", "normal", "high"]
# PROPFIND and REPORT are the WebDAV/CalDAV read methods (RFC 4918, RFC 4791); they are
# safe like GET, so dry-run serves them from fixtures too (MODULES §12.3).
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "PROPFIND", "REPORT"]
SAFE_HTTP_METHODS: frozenset[str] = frozenset({"GET", "PROPFIND", "REPORT"})


class ToolError(Exception):
    """An expected failure the model should hear about (MODULES §5.2).

    ``message`` is shown to the model (≤ 40 words, actionable). Never put a secret or a
    stack trace in it. ``code="reauth"`` also flips the integration status.
    """

    def __init__(
        self, message: str, *, code: ToolErrorCode = "other", retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code: ToolErrorCode = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Return this when the text the model sees should differ from the spoken line."""

    structured: dict[str, Any]
    text: str | None = None

    @property
    def speak(self) -> str:
        value = self.structured.get("speak")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class Health:
    status: HealthStatus
    detail: str = ""
    spoken: str | None = None


@dataclass(frozen=True, slots=True)
class PromptBlock:
    """A stable, cacheable system-prompt block from a persona module (MODULES §5.6)."""

    text: str


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    id: str
    name: str
    kind: ProfileKind
    locale: str = "en-US"
    tts_voice: str | None = None
    speech_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class EntityRef:
    """One HA entity as the resolver and ``ctx.ha`` see it (HOME_ASSISTANT §6.1)."""

    uuid: str
    entity_id: str
    domain: str
    name: str
    aliases: tuple[str, ...] = ()
    area_id: str | None = None
    floor_id: str | None = None
    device_id: str | None = None
    config_entry_id: str | None = None
    device_class: str | None = None
    hidden: bool = False
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class EntityState:
    entity: EntityRef
    state: str
    attributes: Mapping[str, Any] = field(default_factory=dict[str, Any])
    last_changed: str | None = None
    #: True when HA is unreachable and this comes from the last known cache (HOME_ASSISTANT §4).
    stale: bool = False


@dataclass(frozen=True, slots=True)
class ServiceResult:
    ok: bool
    detail: str | None = None


# ----------------------------------------------------------------- protocols --


@runtime_checkable
class KVStore(Protocol):
    """Scoped key/value (MODULES §5.3): JSON values ≤ 64 KiB, ≤ 1 MiB per scope."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def keys(self, prefix: str = "") -> list[str]: ...
    def household(self) -> KVStore: ...


@runtime_checkable
class SecretStore(Protocol):
    """Module- and profile-scoped secrets, decrypted lazily (MODULES §5.3, §7)."""

    async def get(self, name: str) -> str | None: ...
    async def set(self, name: str, value: str) -> None: ...
    async def delete(self, name: str) -> None: ...


@runtime_checkable
class HAClient(Protocol):
    """The only module-facing HA client (MODULES §5.3; mechanics in HOME_ASSISTANT §8).

    Granted entities only. Outside a tool call ``call_service`` raises.
    """

    async def resolve(
        self, target: str, *, domains: list[str] | None = None, area: str | None = None
    ) -> list[EntityRef]: ...
    async def state(self, entity: EntityRef | str) -> EntityState | None: ...
    async def states(self, domains: list[str] | None = None) -> list[EntityState]: ...
    async def call_service(
        self,
        domain: str,
        service: str,
        targets: list[EntityRef],
        data: dict[str, Any] | None = None,
    ) -> ServiceResult: ...


@runtime_checkable
class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def headers(self) -> Mapping[str, str]: ...
    def json(self) -> Any: ...


@runtime_checkable
class HttpClient(Protocol):
    """Thin async HTTP client limited to the manifest's ``egress`` hosts (MODULES §5.3)."""

    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...
    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...
    async def put(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...
    async def patch(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...
    async def delete(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...


@runtime_checkable
class GrantView(Protocol):
    """Read-only view of the caller's effective grants (MODULES §5.3)."""

    def allows(self, resource: str, risk: Risk) -> bool: ...
    def max_risk(self, resource: str) -> Risk | None: ...
    def resources(
        self, kind: Literal["tool", "entity", "area", "config_entry"]
    ) -> Sequence[str]: ...


class Announce(Protocol):
    """``await ctx.announce(text, profile_id=None, priority="normal")`` (MODULES §9)."""

    def __call__(
        self, text: str, *, profile_id: str | None = None, priority: AnnouncePriority = "normal"
    ) -> Awaitable[None]: ...


type Spawn = Callable[[Coroutine[Any, Any, Any]], Any]
type ToolHandler = Callable[..., Awaitable[dict[str, Any] | ToolResult]]
type ToolArgs = Mapping[str, Any]
__all__ = [
    "SAFE_HTTP_METHODS",
    "Announce",
    "AnnouncePriority",
    "EntityRef",
    "EntityState",
    "GrantView",
    "HAClient",
    "Health",
    "HealthStatus",
    "HttpClient",
    "HttpMethod",
    "HttpResponse",
    "KVStore",
    "Principal",
    "ProfileSummary",
    "PromptBlock",
    "Risk",
    "SecretStore",
    "ServiceResult",
    "Spawn",
    "ToolArgs",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolResult",
]
