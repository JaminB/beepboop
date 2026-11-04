"""``ToolContext``: the one context type (docs/MODULES.md §5.3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import Any

from structlog.typing import FilteringBoundLogger

from beepboop.module.types import (
    Announce,
    GrantView,
    HAClient,
    HttpClient,
    KVStore,
    ProfileSummary,
    SecretStore,
    Spawn,
)
from beepboop.policy.types import Approval, Principal


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Services plus, inside a tool call, the caller.

    ``principal``, ``grants``, ``turn_id``, ``deadline`` and ``approval`` are ``None`` when a
    hook (``setup``, ``health``, ``teardown``, ``prompt_block``) or a spawned task runs, and
    ``profile_id`` is ``None`` for household-scope hooks. Core builds these; modules only
    read them.
    """

    module_id: str
    profile_id: str | None
    profile: ProfileSummary | None
    settings: Mapping[str, Any]
    secrets: SecretStore
    ha: HAClient
    http: HttpClient
    kv: KVStore
    log: FilteringBoundLogger
    locale: str
    tz: tzinfo
    now: Callable[[], datetime]
    dry_run: bool
    spawn: Spawn
    announce: Announce

    principal: Principal | None = None
    grants: GrantView | None = None
    turn_id: str | None = None
    deadline: float | None = None
    approval: Approval | None = None

    def with_call(
        self,
        *,
        principal: Principal,
        grants: GrantView | None,
        turn_id: str,
        deadline: float | None,
        approval: Approval | None = None,
    ) -> ToolContext:
        """The same services, bound to one caller (what the registry hands a handler)."""
        return replace(
            self,
            principal=principal,
            grants=grants,
            turn_id=turn_id,
            deadline=deadline,
            approval=approval,
        )

    @property
    def in_tool_call(self) -> bool:
        return self.principal is not None


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ContextSpec:
    """What a ``ContextFactory`` needs to build a hook context for one integration row."""

    module_id: str
    profile_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict[str, Any])


__all__ = ["ContextSpec", "ToolContext", "utc_now"]
