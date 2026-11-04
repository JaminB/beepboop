"""Policy vocabulary: risk tiers, the Principal, targets, approvals and decisions.

Code form of docs/IDENTITY_AND_TRUST.md §3, §4 and §7.1. The doc is the contract; the
literal strings here (tiers, deny reasons, factors) are the only vocabulary the rest of
core may use, and ``tool_calls.deny_reason`` in the database uses the same strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, get_args

Risk = Literal["read", "act", "personal", "critical"]
ProfileKind = Literal["admin", "member", "child", "guest"]
Via = Literal["voice", "web", "oauth", "pat"]
Factor = Literal["web", "pin", "phone", "elicit"]
Outcome = Literal["allow", "deny", "confirm"]
DenyReason = Literal[
    "ceiling",
    "no_grant",
    "schedule",
    "scope",
    "service_blocked",
    "disabled",
    "rate_limited",
    "identity",
    "impersonation",
    "no_factor",
    "approval_invalid",
]

RISKS: tuple[Risk, ...] = get_args(Risk)
DENY_REASONS: tuple[DenyReason, ...] = get_args(DenyReason)
FACTORS: tuple[Factor, ...] = get_args(Factor)
PROFILE_KINDS: tuple[ProfileKind, ...] = get_args(ProfileKind)

#: Tier order (IDENTITY_AND_TRUST §7.1). Tiers are totally ordered.
TIER: dict[Risk, int] = {"read": 0, "act": 1, "personal": 2, "critical": 3}
#: External-client scope needed for each tier (§7.1).
SCOPE_FOR: dict[Risk, str] = {
    "read": "home.read",
    "act": "home.control",
    "personal": "personal",
    "critical": "critical",
}
#: Scopes a voice or web principal always carries (§3).
ALL_SCOPES: frozenset[str] = frozenset(SCOPE_FOR.values())
#: The built-in Guest profile id (DATA_AND_PRIVACY §8).
GUEST_PROFILE_ID = "guest"


def max_tier(*risks: Risk) -> Risk:
    """The highest of ``risks`` (at least one required). Policy only ever raises a tier."""
    if not risks:
        msg = "max_tier needs at least one risk"
        raise ValueError(msg)
    return max(risks, key=lambda r: TIER[r])


def is_risk(value: object) -> bool:
    """True if ``value`` is one of the four tier strings."""
    return isinstance(value, str) and value in TIER


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, built once by the front door and immutable for the call (§3).

    Model output can never change a Principal; modules only read it through
    ``ToolContext.principal``.
    """

    profile_id: str
    via: Via
    confidence: float | None
    margin: float | None
    device: str
    scopes: frozenset[str]
    tainted: bool
    turn_id: str
    actor_profile_id: str | None = None

    @property
    def is_guest(self) -> bool:
        return self.profile_id == GUEST_PROFILE_ID

    @property
    def is_external(self) -> bool:
        return self.via in ("oauth", "pat")


@dataclass(frozen=True, slots=True)
class Target:
    """One HA entity a call touches, produced by the resolver (§7.1).

    ``service`` is ``None`` for pure reads (``home.state``) where no service is called.
    """

    uuid: str
    domain: str
    service: str | None = None
    device_class: str | None = None
    declared_risk: Risk | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """The policy view of one ``approvals`` row, passed back in on re-dispatch (§7.1)."""

    id: str
    profile_id: str | None
    tool: str
    args_digest: str
    exp: datetime
    decision: Literal["approved", "denied", "expired"] | None = None
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of ``authorize()``: deny beats confirm beats allow (§7.2)."""

    outcome: Outcome
    effective_risk: Risk
    reason: DenyReason | None = None
    factors: tuple[Factor, ...] = ()
    resource: str | None = None
    grant_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class ToolLike(Protocol):
    """What policy needs to know about a tool: its name and declared tier."""

    @property
    def name(self) -> str: ...

    @property
    def risk(self) -> Risk: ...


@dataclass(frozen=True, slots=True)
class SimpleTool:
    """A minimal :class:`ToolLike`, for tests and ``policy explain``."""

    name: str
    risk: Risk


@dataclass(frozen=True, slots=True)
class Calibration:
    """Per-profile speaker thresholds (VOICE_PIPELINE §8.4 owns the defaults)."""

    tau_act: float = 0.70
    margin_act: float = 0.10
    tau_personal: float = 0.90
    margin_personal: float = 0.20


DEFAULT_CALIBRATION = Calibration()


@dataclass(frozen=True, slots=True)
class ProfileView:
    """The slice of a ``profiles`` row that policy reads (§2)."""

    id: str
    kind: ProfileKind
    risk_ceiling: Risk
    group_ids: frozenset[str] = frozenset()
    disabled: bool = False
    has_login: bool = False
    pin_enabled: bool = False
    pin_locked: bool = False
    calibration: Calibration = DEFAULT_CALIBRATION


@dataclass(frozen=True, slots=True)
class EntityInfo:
    """The slice of an ``ha_entities`` row that grants are matched against (§5, §6)."""

    uuid: str
    status: Literal["new", "active", "hidden"] = "active"
    area_id: str | None = None
    config_entry_id: str | None = None
    #: The inbox tier for indirect services (``ha_entities.declared_risk``); the HA target
    #: extractor copies it into ``Target.declared_risk``.
    declared_risk: Risk | None = None
