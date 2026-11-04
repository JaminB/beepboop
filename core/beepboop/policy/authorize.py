"""``policy.authorize``: the only gate (IDENTITY_AND_TRUST §7, CLAUDE.md hard rule 1).

A pure function over a :class:`PolicyState` snapshot and its arguments. No I/O. The
registry calls it before every handler, whichever door the call came through; the fast
path uses the very same function. The algorithm follows §7.2 line by line, in order:
deny beats confirm beats allow, and nothing ever lowers a tier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Literal

from beepboop.policy.digest import args_digest
from beepboop.policy.grants import GrantIndex, effective_grants
from beepboop.policy.ha_services import ServiceTable, load_service_table
from beepboop.policy.ratelimit import RateLimiter
from beepboop.policy.types import (
    SCOPE_FOR,
    TIER,
    Approval,
    Decision,
    DenyReason,
    EntityInfo,
    Factor,
    Principal,
    ProfileView,
    Risk,
    Target,
    ToolLike,
    max_tier,
)


@dataclass
class PolicyState:
    """Everything ``authorize`` reads. Rebuilt (not mutated) when the database changes.

    ``rate_limiter`` is the one stateful member and is shared across rebuilds.
    """

    profiles: Mapping[str, ProfileView]
    grants: GrantIndex = field(default_factory=GrantIndex)
    entities: Mapping[str, EntityInfo] = field(default_factory=dict[str, EntityInfo])
    services: ServiceTable = field(default_factory=load_service_table)
    rate_limiter: RateLimiter | None = None
    tz: tzinfo = UTC
    #: System > Voice "ask on phone when unsure" (WEB_UI §5.11): a ``personal`` call below
    #: τ_strict asks for the second factor (True) or is denied as ``identity`` (False).
    ask_when_unsure: bool = True


def _deny(reason: DenyReason, risk: Risk, resource: str | None = None) -> Decision:
    return Decision("deny", risk, reason=reason, resource=resource)


def _allow(risk: Risk, grant_id: str | None) -> Decision:
    return Decision("allow", risk, grant_id=grant_id)


def _confirm(risk: Risk, factors: tuple[Factor, ...], grant_id: str | None) -> Decision:
    return Decision("confirm", risk, factors=factors, grant_id=grant_id)


def available_factors(p: Principal, profile: ProfileView) -> tuple[Factor, ...]:
    """§7.2 step 7 / §8.4: voice offers web (if a login exists) and PIN (if enabled, not
    locked, conversation not tainted); web offers web; external clients offer elicit."""
    if p.via == "voice":
        factors: list[Factor] = []
        if profile.has_login:
            factors.append("web")
        if profile.pin_enabled and not profile.pin_locked and not p.tainted:
            factors.append("pin")
        return tuple(factors)
    if p.via == "web":
        return ("web",) if profile.has_login else ()
    return ("elicit",)


def approver_of(p: Principal) -> str:
    """Whose approval a call needs (§3.1, §8.2): the profile itself, or the acting admin
    for a *test as* call (the approval was created for the actor, so only the actor's
    approval may satisfy it)."""
    return p.actor_profile_id or p.profile_id


def approval_bound(approval: Approval, p: Principal, tool_name: str, digest: str) -> bool:
    """§8.2 binding: same approver, same tool, same request (``args_digest``)."""
    return (
        approval.profile_id == approver_of(p)
        and approval.tool == tool_name
        and approval.args_digest == digest
    )


def effective_risk(
    tool: ToolLike, targets: list[Target] | tuple[Target, ...], services: ServiceTable
) -> Risk | Literal["blocked"]:
    """§7.2 step 1: the tool's tier raised by every target. ``"blocked"`` for a blocked service."""
    risk: Risk = tool.risk
    for t in targets:
        if services.is_blocked(t.domain, t.service):
            return "blocked"
        risk = max_tier(
            risk, services.risk(t.domain, t.service, t.device_class), t.declared_risk or "read"
        )
    return risk


def authorize(
    state: PolicyState,
    p: Principal,
    tool: ToolLike,
    args: Mapping[str, object],
    targets: list[Target] | tuple[Target, ...],
    now: datetime,
    approval: Approval | None = None,
) -> Decision:
    """Decide ``allow`` / ``deny(reason)`` / ``confirm(factors)`` for one call (§7.2)."""
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    profile = state.profiles.get(p.profile_id)
    if profile is None or profile.disabled:
        return _deny("disabled", tool.risk)
    if p.actor_profile_id is not None and profile.kind in ("admin", "member"):
        return _deny("impersonation", tool.risk)

    # 1. Effective risk: raised, never lowered.
    risk = effective_risk(tool, targets, state.services)
    if risk == "blocked":
        return _deny("service_blocked", tool.risk)

    if state.rate_limiter is not None and state.rate_limiter.exceeded(
        p.profile_id, p.device, tool.name, risk, now
    ):
        return _deny("rate_limited", risk)

    # 2. Ceiling.
    if TIER[risk] > TIER[profile.risk_ceiling]:
        return _deny("ceiling", risk)

    # 3. Grants: every resource covered by an active, strong-enough grant.
    active = effective_grants(state.grants, profile.id, profile.group_ids, risk, now, state.tz)
    tool_grant = next((g for g in active if g.covers_tool(tool.name)), None)
    if tool_grant is None:
        reason: DenyReason = (
            "schedule"
            if any(
                g.covers_tool(tool.name) and g.strong_enough(risk)
                for g in state.grants.for_subjects(profile.id, profile.group_ids)
            )
            else "no_grant"
        )
        return _deny(reason, risk)
    for t in targets:
        entity = state.entities.get(t.uuid, EntityInfo(uuid=t.uuid, status="new"))
        if not any(g.covers_entity(entity) for g in active):
            return _deny("no_grant", risk, resource=t.uuid)

    # 4. External clients: scopes cap, never add.
    if p.is_external and SCOPE_FOR[risk] not in p.scopes:
        return _deny("scope", risk)

    # 5. Identity requirement by tier.
    needs_factor = False
    if risk == "critical":
        needs_factor = True
    elif p.via == "voice" and not p.is_guest:
        cal = profile.calibration
        conf = p.confidence if p.confidence is not None else 0.0
        margin = p.margin if p.margin is not None else 0.0
        if risk == "personal":
            if not (conf >= cal.tau_personal and margin >= cal.margin_personal):
                if not state.ask_when_unsure:
                    return _deny("identity", risk)
                needs_factor = True
        elif risk == "act" and not (conf >= cal.tau_act and margin >= cal.margin_act):
            return _deny("identity", risk)

    # 6. Taint.
    if p.tainted and TIER[risk] > TIER["read"]:
        needs_factor = True

    if not needs_factor:
        return _allow(risk, tool_grant.id)

    # 7. Second factor.
    if approval is not None:
        ok = (
            approval.decision == "approved"
            and approval.executed_at is None
            and approval.exp > now
            and approval_bound(
                approval, p, tool.name, args_digest(tool.name, args, (t.uuid for t in targets))
            )
        )
        return _allow(risk, tool_grant.id) if ok else _deny("approval_invalid", risk)
    # The factor belongs to whoever approves: the actor for a *test as* call (§3.1).
    factors = available_factors(p, state.profiles.get(approver_of(p)) or profile)
    if not factors:
        return _deny("no_factor", risk)
    return _confirm(risk, factors, tool_grant.id)
