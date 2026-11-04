"""``/api/integrations``: module cards, settings forms, secrets, and the HA new-device
inbox (WEB_UI §5.5, §5.6; MODULES §6, §7).

Cards and forms come straight from ``manifest.yaml``: no per-module UI or API code.
Settings are validated against ``manifest.settings`` (the MODULES §6.1 subset) on save;
secrets are write-only and come back as ``{"set": true}``. *Test and save* saves, then
calls ``Module.health(ctx)`` and records the status on the integration row.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Literal, Protocol, cast, runtime_checkable

import jsonschema
from fastapi import APIRouter
from pydantic import BaseModel, Field

from beepboop.api.deps import Admin, ApiDeps, Deps, Session, WebSession, record_audit
from beepboop.api.people import InboxDecision, apply_inbox, entity_default_risk, undo_inbox
from beepboop.api.problem import ApiError
from beepboop.db.ids import new_ulid
from beepboop.db.repo import ha_entities, integrations
from beepboop.log import get_logger
from beepboop.module.context import ToolContext
from beepboop.policy.grants import effective_grants
from beepboop.policy.ha_services import load_service_table
from beepboop.policy.types import Risk
from beepboop.registry.manifest import Manifest
from beepboop.secrets import SecretRef

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

HEALTH_TIMEOUT_S = 10.0


@runtime_checkable
class HookContexts(Protocol):
    """The part of ``CoreContextFactory`` that builds hook contexts (MODULES §5.3)."""

    def for_hook(self, module_id: str, profile_id: str | None = None) -> ToolContext: ...


class SecretStatus(BaseModel):
    name: str
    title: str | None
    description: str | None
    required: bool
    set: bool


class ToolInfo(BaseModel):
    name: str
    risk: str
    description: str


class ConnectionView(BaseModel):
    profile_id: str | None
    status: str
    last_error: str | None
    settings: dict[str, Any]
    secrets: list[SecretStatus]


class IntegrationCard(BaseModel):
    module_id: str
    #: Human name ("Calendar"); the id stays in Details (WEB_UI §11.2).
    title: str
    summary: str
    scope: str
    category: str
    version: str
    status: str
    statuses: dict[str, str]


class IntegrationDetail(IntegrationCard):
    settings_schema: dict[str, Any]
    household: ConnectionView | None
    mine: ConnectionView | None
    tools: list[ToolInfo]
    egress: list[dict[str, str | None]]
    #: The human-facing SETUP.md (WEB_UI §5.6 "Setup guide" fold); empty when absent.
    setup_md: str = ""
    #: Household-module fields each person may override (MODULES §6.3, ``x-per-profile``).
    per_profile_keys: list[str] = Field(default_factory=list[str])


class EntityOption(BaseModel):
    """One granted HA entity for an ``x-entity`` picker (MODULES §6.1)."""

    uuid: str
    name: str
    entity_id: str
    domain: str
    area_id: str | None


class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict[str, Any])
    secrets: dict[str, str] = Field(default_factory=dict[str, str])
    profile_id: str | None = None


def _manifest(deps: ApiDeps, module_id: str) -> Manifest:
    m = deps.registry.manifest(module_id)
    if m is None:
        raise ApiError(404, "not_found")
    return m


def _status_word(rows: list[integrations.IntegrationRow], scope: str, profile_id: str) -> str:
    if scope == "household":
        return rows[0].status if rows else "ok"
    mine = next((r for r in rows if r.profile_id == profile_id), None)
    return "off" if mine is None else mine.status


async def _secret_status(deps: ApiDeps, m: Manifest, profile_id: str | None) -> list[SecretStatus]:
    out: list[SecretStatus] = []
    for s in m.secrets:
        present = False
        if deps.secrets is not None:
            present = await deps.secrets.exists(
                SecretRef(s.name, owner_module=m.id, profile_id=profile_id)
            )
        out.append(
            SecretStatus(
                name=s.name,
                title=s.title,
                description=s.description,
                required=s.required,
                set=present,
            )
        )
    return out


async def _connection(deps: ApiDeps, m: Manifest, profile_id: str | None) -> ConnectionView:
    row = await deps.db.read(lambda c: integrations.get(c, m.id, profile_id))
    return ConnectionView(
        profile_id=profile_id,
        status="off" if row is None else row.status,
        last_error=None if row is None else row.last_error,
        settings={} if row is None else dict(row.settings),
        secrets=await _secret_status(deps, m, profile_id),
    )


def _visible_statuses(
    session: WebSession, rows: list[integrations.IntegrationRow]
) -> dict[str, str]:
    """Admins see every person's connection status; others only their own (§6.4)."""
    out: dict[str, str] = {}
    for r in rows:
        key = r.profile_id or "household"
        if session.is_admin or r.profile_id in (None, session.profile.id):
            out[key] = r.status
    return out


@router.get("", operation_id="integrations_list")
async def integrations_list(session: Session, deps: Deps) -> list[IntegrationCard]:
    if session.profile.kind == "child":
        return []
    cards: list[IntegrationCard] = []
    for m, _module in deps.registry.modules():
        rows = await deps.db.read(lambda c, mid=m.id: integrations.list_for_module(c, mid))
        cards.append(
            IntegrationCard(
                module_id=m.id,
                title=m.display_title,
                summary=m.summary,
                scope=m.scope,
                category=m.category,
                version=m.version,
                status=_status_word(rows, m.scope, session.profile.id),
                statuses=_visible_statuses(session, rows),
            )
        )
    return cards


@router.get("/{module_id}", operation_id="integrations_read")
async def integrations_read(module_id: str, session: Session, deps: Deps) -> IntegrationDetail:
    if session.profile.kind == "child":
        raise ApiError(404, "not_found")
    m = _manifest(deps, module_id)
    rows = await deps.db.read(lambda c: integrations.list_for_module(c, m.id))
    return IntegrationDetail(
        module_id=m.id,
        title=m.display_title,
        summary=m.summary,
        scope=m.scope,
        category=m.category,
        version=m.version,
        status=_status_word(rows, m.scope, session.profile.id),
        statuses=_visible_statuses(session, rows),
        settings_schema=dict(m.settings),
        household=await _connection(deps, m, None) if session.is_admin else None,
        mine=(
            await _connection(deps, m, session.profile.id)
            if m.scope == "profile" or per_profile_keys(m)
            else None
        ),
        tools=[ToolInfo(name=t.name, risk=t.risk, description=t.description) for t in m.tools],
        egress=[
            {"destination": e.destination, "purpose": e.purpose, "data": e.data} for e in m.egress
        ],
        setup_md=deps.registry.setup_guide(m.id) or "",
        per_profile_keys=per_profile_keys(m),
    )


def per_profile_keys(m: Manifest) -> list[str]:
    """Settings a household module lets each profile override (``x-per-profile: true``)."""
    props = m.settings.get("properties")
    if not isinstance(props, dict):
        return []
    out: list[str] = []
    for key, spec in cast(dict[str, object], props).items():
        if isinstance(spec, dict) and cast(dict[str, object], spec).get("x-per-profile") is True:
            out.append(str(key))
    return out


@router.get("/{module_id}/entities", operation_id="integrations_entities")
async def integrations_entities(
    module_id: str, session: Session, deps: Deps, domains: str | None = None
) -> list[EntityOption]:
    """Granted HA entities for an ``x-entity`` setting (MODULES §6.1, WEB_UI §5.6): what
    the signed-in person may use, optionally limited to ``domains`` (comma-separated)."""
    _manifest(deps, module_id)
    wanted = {d.strip() for d in (domains or "").split(",") if d.strip()}
    state = deps.policy_state()
    profile = state.profiles.get(session.profile.id)
    if profile is None:
        return []
    active = effective_grants(
        state.grants, profile.id, profile.group_ids, "read", deps.clock(), state.tz
    )
    rows = await deps.db.read(lambda c: ha_entities.list_(c, "active"))
    out: list[EntityOption] = []
    for e in rows:
        if e.removed_at is not None or (wanted and e.domain not in wanted):
            continue
        info = state.entities.get(e.uuid)
        if info is None or not any(g.covers_entity(info) for g in active):
            continue
        out.append(
            EntityOption(
                uuid=e.uuid,
                name=e.friendly_name or e.entity_id,
                entity_id=e.entity_id,
                domain=e.domain,
                area_id=e.area_id,
            )
        )
    return sorted(out, key=lambda o: (o.area_id or "~", o.name))


def validate_settings(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    """Server-side validation with the same schema the form renders (WEB_UI §9)."""
    errors: dict[str, str] = {}
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for err in validator.iter_errors(values):  # pyright: ignore[reportUnknownMemberType] - stubs overload
        path = ".".join(str(p) for p in err.absolute_path)
        if not path and err.validator == "required":
            missing = cast(list[str], err.validator_value)
            for name in missing:
                if name not in values:
                    errors[name] = "required"
            continue
        errors[path or "_"] = err.message
    return errors


async def run_health(
    deps: ApiDeps, m: Manifest, profile_id: str | None
) -> tuple[Literal["ok", "reauth", "error"], str]:
    entry = next(((mm, mod) for mm, mod in deps.registry.modules() if mm.id == m.id), None)
    factory = deps.registry.context_factory
    if entry is None or not isinstance(factory, HookContexts):
        return "ok", ""
    ctx = factory.for_hook(m.id, profile_id)
    try:
        health = await asyncio.wait_for(entry[1].health(ctx), HEALTH_TIMEOUT_S)
    except TimeoutError:
        return "error", "Health check timed out."
    except Exception as exc:
        # WEB_UI §1.4: say what failed and what next; the trace id finds the log line.
        trace_id = new_ulid()
        get_logger(__name__).exception(
            "integration.health_failed",
            module_id=m.id,
            profile_id=profile_id,
            trace_id=trace_id,
            error=type(exc).__name__,
        )
        return (
            "error",
            f"The {m.display_title} check failed unexpectedly. Details are in System > Logs"
            f" (trace {trace_id}).",
        )
    status: Literal["ok", "reauth", "error"] = (
        "ok" if health.status in ("ok", "degraded") else health.status
    )
    return status, health.detail


@router.put("/{module_id}/settings", operation_id="integrations_save")
async def integrations_save(
    module_id: str, body: SettingsBody, session: Session, deps: Deps
) -> ConnectionView:
    """*Test and save*: validate, store settings and secrets, then a real health check."""
    m = _manifest(deps, module_id)
    overridable = per_profile_keys(m)
    if m.scope == "household" and body.profile_id is not None:
        # MODULES §6.3: "Your settings" on a household module: only x-per-profile keys,
        # stored on the person's own row; secrets stay household-only.
        if body.profile_id != session.profile.id and not session.is_admin:
            raise ApiError(403, "forbidden")
        if session.profile.kind == "child":
            raise ApiError(403, "forbidden")
        bad = sorted(set(body.values) - set(overridable))
        if bad or body.secrets:
            raise ApiError(
                422, "invalid", fields=dict.fromkeys(bad or ["secrets"], "not_per_profile")
            )
        sub_schema = {
            "type": "object",
            "properties": {
                k: v
                for k, v in cast(dict[str, Any], m.settings.get("properties", {})).items()
                if k in overridable
            },
        }
        errors = validate_settings(sub_schema, body.values)
        if errors:
            raise ApiError(422, "invalid", fields=errors)
        pid = body.profile_id
        # The override row is per person (scope 'profile' with a profile id; the household
        # row keeps scope 'household' and no profile id, as the table's CHECK requires).
        await deps.db.write(
            lambda c: integrations.upsert(
                c, m.id, "profile", pid, settings=body.values, status="ok"
            ),
            durable=True,
        )
        await deps.refresh_policy()
        await record_audit(
            deps,
            session.profile,
            "integration.saved",
            target=m.id,
            details={"profile_id": pid, "keys": sorted(body.values)},
        )
        return await _connection(deps, m, pid)
    if m.scope == "household":
        if not session.is_admin:
            raise ApiError(403, "admin_only")
        profile_id: str | None = None
    else:
        profile_id = body.profile_id or session.profile.id
        if profile_id != session.profile.id and not session.is_admin:
            raise ApiError(403, "forbidden")
        if session.profile.kind == "child":
            raise ApiError(403, "forbidden")
    errors = validate_settings(dict(m.settings), body.values)
    unknown = sorted(set(body.secrets) - {s.name for s in m.secrets})
    for name in unknown:
        errors[name] = "unknown_secret"
    if errors:
        raise ApiError(422, "invalid", fields=errors)
    if body.secrets:
        if deps.secrets is None:
            raise ApiError(503, "secrets_unavailable")
        for name, value in body.secrets.items():
            if value:
                await deps.secrets.put(
                    SecretRef(name, owner_module=m.id, profile_id=profile_id), value
                )
    await deps.db.write(
        lambda c: integrations.upsert(
            c, m.id, m.scope, profile_id, settings=body.values, status="ok"
        ),
        durable=True,
    )
    await deps.refresh_policy()  # core reloads saved settings into ctx.settings
    status, detail = await run_health(deps, m, profile_id)
    await deps.db.write(
        lambda c: integrations.upsert(
            c,
            m.id,
            m.scope,
            profile_id,
            status=status,
            last_error=None if status == "ok" else detail,
        ),
        durable=True,
    )
    await record_audit(
        deps,
        session.profile,
        "integration.saved",
        target=m.id,
        details={"profile_id": profile_id, "status": status},
    )
    view = await _connection(deps, m, profile_id)
    view.last_error = detail or view.last_error
    return view


@router.delete(
    "/{module_id}/secrets/{name}", operation_id="integrations_secret_clear", status_code=204
)
async def integrations_secret_clear(
    module_id: str, name: str, session: Session, deps: Deps, profile_id: str | None = None
) -> None:
    m = _manifest(deps, module_id)
    pid = None if m.scope == "household" else (profile_id or session.profile.id)
    if (m.scope == "household" or pid != session.profile.id) and not session.is_admin:
        raise ApiError(403, "forbidden")
    if deps.secrets is None:
        raise ApiError(503, "secrets_unavailable")
    await deps.secrets.delete(SecretRef(name, owner_module=m.id, profile_id=pid))
    await record_audit(deps, session.profile, "integration.secret_cleared", target=m.id)


# ------------------------------------------------------------------ inbox --


class InboxItem(BaseModel):
    uuid: str
    entity_id: str
    name: str
    domain: str
    device_class: str | None
    area_id: str | None
    risk: Risk
    lowerable: bool
    default_choice: Literal["everyone", "adults"]
    first_seen_at: int


class InboxDecisionBody(BaseModel):
    uuid: str
    choice: Literal["everyone", "adults", "only", "ignore"]
    profile_ids: list[str] = Field(default_factory=list[str])
    risk: Risk | None = None


class InboxApply(BaseModel):
    decisions: list[InboxDecisionBody] = Field(max_length=500)


class InboxApplied(BaseModel):
    grants_written: int
    remaining: int


class InboxUndo(BaseModel):
    uuids: list[str]


async def _inbox_count(deps: ApiDeps) -> int:
    row = await deps.db.read(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM ha_entities WHERE status = 'new' AND removed_at IS NULL"
        ).fetchone()
    )
    return 0 if row is None else int(row[0])


@router.get("/ha/inbox", operation_id="inbox_list")
async def inbox_list(_admin: Admin, deps: Deps) -> list[InboxItem]:
    """New devices: nobody can use them until an admin decides (IDENTITY_AND_TRUST §6.2)."""
    table = load_service_table()
    rows = await deps.db.read(lambda c: ha_entities.list_(c, "new"))
    out: list[InboxItem] = []
    for e in rows:
        if e.removed_at is not None:
            continue
        risk = entity_default_risk(e.domain, e.device_class, table)
        out.append(
            InboxItem(
                uuid=e.uuid,
                entity_id=e.entity_id,
                name=e.friendly_name or e.entity_id,
                domain=e.domain,
                device_class=e.device_class,
                area_id=e.area_id,
                risk=risk,
                lowerable=e.domain in table.indirect,
                default_choice="adults" if risk == "critical" else "everyone",
                first_seen_at=e.first_seen_at,
            )
        )
    return out


@router.post("/ha/inbox", operation_id="inbox_apply")
async def inbox_apply(body: InboxApply, admin: Admin, deps: Deps) -> InboxApplied:
    decisions = [
        InboxDecision(d.uuid, d.choice, tuple(d.profile_ids), d.risk) for d in body.decisions
    ]
    for d in decisions:
        if d.choice == "only" and not d.profile_ids:
            raise ApiError(422, "only_needs_people", fields={d.uuid: "only_needs_people"})

    def _w(c: sqlite3.Connection) -> list[str]:
        return apply_inbox(c, decisions, created_by=admin.profile.id)

    try:
        written = await deps.db.write(_w, durable=True)
    except ValueError as exc:
        raise ApiError(422, "risk_not_lowerable", detail=str(exc)) from exc
    await deps.refresh_policy()
    remaining = await _inbox_count(deps)
    deps.events.publish("inbox", {"count": remaining}, audience="admins")
    await record_audit(
        deps,
        admin.profile,
        "inbox.applied",
        details={"decisions": [{"uuid": d.uuid, "choice": d.choice} for d in decisions]},
    )
    return InboxApplied(grants_written=len(written), remaining=remaining)


@router.post("/ha/inbox/undo", operation_id="inbox_undo")
async def inbox_undo(body: InboxUndo, admin: Admin, deps: Deps) -> InboxApplied:
    """The 6 s *Undo* toast: back to the inbox, grants removed."""
    n = await deps.db.write(lambda c: undo_inbox(c, body.uuids), durable=True)
    await deps.refresh_policy()
    remaining = await _inbox_count(deps)
    deps.events.publish("inbox", {"count": remaining}, audience="admins")
    await record_audit(deps, admin.profile, "inbox.undone", details={"uuids": body.uuids})
    return InboxApplied(grants_written=-n, remaining=remaining)


inbox_count = _inbox_count

__all__ = ["HookContexts", "inbox_count", "router", "validate_settings"]
