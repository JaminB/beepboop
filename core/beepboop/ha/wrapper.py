"""The policy-wrapped HA client: what modules get as ``ctx.ha`` (HOME_ASSISTANT §8,
MODULES §5.3).

Modules never touch :class:`~beepboop.ha.client.HAConnection` (it lives inside
``beepboop.ha``, which holds the token). Each tool call gets its own
:class:`PolicyHAClient` bound to the call's principal, tool and approval; hooks get one
with no principal, which is read-only.

``call_service`` does, in order and deterministically:

1. outside a tool call → ``PolicyDenied``; a **blocked** service →
   ``PolicyDenied("service_blocked")``;
2. every target must be a known entity of ``domain`` **in the principal's granted view**,
   else ``PolicyDenied("no_grant")``; the entity ids sent to HA come from the index, never
   from the module;
3. ``policy.authorize`` is re-run on the actual ``(domain, service, entities)``: ``deny``
   raises, ``confirm`` passes only with the approval that ``registry.dispatch`` already
   verified and consumed for this call, **and** only for the entities that dispatch
   authorized (each at most once), so one approval never widens to other targets or loops;
4. ``data`` may not carry HA targeting keys (``entity_id``, ``area_id``, ``device_id``,
   ``floor_id``, ``label_id``): HA merges ``target`` into the service data, so a module
   could otherwise act on a whole area through ``data``; such a call is ``invalid_args``;
5. ``call_service`` over the WebSocket, ``target`` always set, ``return_response`` only for
   services whose ``get_services`` entry declares a response.

Authorization of the *tool* and the audit row are ``registry.dispatch``'s job; this is the
second guard against a module building its own target list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from beepboop.ha.cache import StateCache
from beepboop.ha.client import HAConnection, HAUnavailableError
from beepboop.ha.index import HAEntityIndex, HookView
from beepboop.ha.plan import HATargetExtractor
from beepboop.ha.ws import HACommandError, HAWebSocketError
from beepboop.log import get_logger
from beepboop.module.types import EntityRef, EntityState, ServiceResult, ToolError
from beepboop.policy.authorize import PolicyState, approval_bound, authorize
from beepboop.policy.errors import PolicyDenied
from beepboop.policy.types import ALL_SCOPES, Approval, Principal, Target, ToolLike
from beepboop.registry.resolver import EntityIndex, TargetQuery, resolve

DEFAULT_CALL_TIMEOUT_S = 5.0
#: Service-data keys HA treats as targets; never accepted from a module (step 4 above).
TARGETING_KEYS = frozenset({"entity_id", "area_id", "device_id", "floor_id", "label_id"})
_HOOK_PRINCIPAL = Principal("__hook__", "web", None, None, "core", ALL_SCOPES, False, "")

type ServiceCatalog = Mapping[str, Mapping[str, object]]


@dataclass
class HARuntime:
    """The live pieces a :class:`PolicyHAClient` reads. Owned by ``HAService``."""

    index: HAEntityIndex
    cache: StateCache
    policy_state: Callable[[], PolicyState]
    connection: HAConnection | None = None
    services: Callable[[], ServiceCatalog] = field(default=lambda: {})
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def connected(self) -> bool:
        return self.connection is not None and self.connection.connected

    @property
    def extractor(self) -> HATargetExtractor:
        return HATargetExtractor(self.index)


def _names(refs: list[EntityRef]) -> str:
    names = [r.name for r in refs]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


class PolicyHAClient:
    """``beepboop.module.HAClient`` bound to one call (or to a hook when ``principal`` is None)."""

    def __init__(
        self,
        runtime: HARuntime,
        *,
        principal: Principal | None = None,
        tool: ToolLike | None = None,
        approval: Approval | None = None,
        deadline: float | None = None,
        authorized_targets: Iterable[str] | None = None,
    ) -> None:
        self._rt = runtime
        self._principal = principal
        self._tool = tool
        self._approval = approval
        self._deadline = deadline
        #: Entity UUIDs ``registry.dispatch`` authorized for this call (``None`` = unknown,
        #: e.g. a test building the client directly); shared with narrowed copies.
        self._authorized: frozenset[str] | None = (
            None if authorized_targets is None else frozenset(authorized_targets)
        )
        #: Targets already acted on under the approval (single use per target).
        self._approved_used: set[str] = set()

    def with_authorized_targets(self, uuids: Iterable[str]) -> PolicyHAClient:
        """The same client narrowed to ``uuids`` (``registry.BindsTargets``)."""
        narrowed = PolicyHAClient(
            self._rt,
            principal=self._principal,
            tool=self._tool,
            approval=self._approval,
            deadline=self._deadline,
            authorized_targets=uuids,
        )
        narrowed._approved_used = self._approved_used
        return narrowed

    # ------------------------------------------------------------------- reads --

    def _view(self) -> tuple[EntityIndex, Principal]:
        if self._principal is None:
            return HookView(self._rt.index), _HOOK_PRINCIPAL
        return self._rt.index, self._principal

    def _visible_uuids(self) -> set[str]:
        view, who = self._view()
        return {e.uuid for e in view.visible(who)}

    def _entity_state(self, ref: EntityRef) -> EntityState | None:
        found = self._rt.cache.get(ref.entity_id)
        if found is None:
            return None
        return EntityState(
            ref, found.state, dict(found.attributes), found.last_changed, not self._rt.connected
        )

    async def resolve(
        self, target: str, *, domains: list[str] | None = None, area: str | None = None
    ) -> list[EntityRef]:
        """Granted entities matching the words; ``ToolError(code="ambiguous")`` when several
        distinct devices match a singular name, so the model asks which one."""
        view, who = self._view()
        domain = domains[0] if domains and len(domains) == 1 else None
        query = TargetQuery(name=target or None, area=area or None, domain=domain)
        result = resolve(who, query, view)
        if result.status == "ambiguous":
            options = [ref for ref, _ in result.candidates]
            raise ToolError(f"Which one do you mean: {_names(options)}?", code="ambiguous")
        if result.status != "ok":
            return []
        return [r for r in result.targets if not domains or r.domain in domains]

    async def state(self, entity: EntityRef | str) -> EntityState | None:
        key = entity.uuid if isinstance(entity, EntityRef) else entity
        item = self._rt.index.registry.get(key)
        if item is None or item.ref.uuid not in self._visible_uuids():
            return None
        return self._entity_state(item.ref)

    async def states(self, domains: list[str] | None = None) -> list[EntityState]:
        view, who = self._view()
        out: list[EntityState] = []
        for ref in view.visible(who):
            if domains and ref.domain not in domains:
                continue
            st = self._entity_state(ref)
            if st is not None:
                out.append(st)
        return out

    # ------------------------------------------------------------------ action --

    async def call_service(
        self,
        domain: str,
        service: str,
        targets: list[EntityRef],
        data: dict[str, Any] | None = None,
    ) -> ServiceResult:
        principal, tool = self._principal, self._tool
        label = f"{domain}.{service}"
        if principal is None or tool is None:
            raise PolicyDenied("no_grant", "act", tool=label)
        state = self._rt.policy_state()
        if state.services.is_blocked(domain, service):
            get_logger(__name__).warning("ha.service_not_allowed", service=label)
            raise PolicyDenied("service_blocked", tool.risk, tool=tool.name)
        if not targets:
            raise ToolError("No device to act on.", code="invalid_args")
        if data and (bad := sorted(TARGETING_KEYS & set(data))):
            raise ToolError(
                f"Service data may not target entities ({', '.join(bad)}); use targets.",
                code="invalid_args",
            )

        visible = self._visible_uuids()
        extractor = self._rt.extractor
        entity_ids: list[str] = []
        policy_targets: list[Target] = []
        for given in targets:
            item = self._rt.index.registry.get(given.uuid)
            if item is None or item.ref.uuid not in visible:
                raise PolicyDenied("no_grant", tool.risk, resource=given.uuid, tool=tool.name)
            ref = item.ref
            if ref.domain != domain:
                raise ToolError(f"{ref.name} is not a {domain}.", code="invalid_args")
            policy_targets.append(extractor.target_with_service(ref, service))
            entity_ids.append(ref.entity_id)

        # Re-check on what is really about to happen. The rate limiter already counted this
        # call in dispatch, so it is left out of the re-check.
        decision = authorize(
            replace(state, rate_limiter=None),
            principal,
            tool,
            {},
            policy_targets,
            self._rt.clock(),
        )
        if decision.outcome == "deny":
            raise PolicyDenied(
                decision.reason or "no_grant",
                decision.effective_risk,
                resource=decision.resource,
                tool=tool.name,
            )
        if decision.outcome == "confirm":
            approval = self._approval
            uuids = [t.uuid for t in policy_targets]
            if not (
                approval is not None
                and approval.decision == "approved"
                and approval_bound(approval, principal, tool.name, approval.args_digest)
                and self._authorized is not None
                and all(u in self._authorized and u not in self._approved_used for u in uuids)
            ):
                raise PolicyDenied("approval_invalid", decision.effective_risk, tool=tool.name)
            self._approved_used.update(uuids)

        conn = self._rt.connection
        if conn is None or not conn.connected:
            raise ToolError("I can't reach the home right now.", code="unavailable", retryable=True)
        catalog = self._rt.services()
        return_response = False
        if catalog:
            known = catalog.get(domain, {})
            if service not in known:
                raise ToolError(f"Home Assistant has no {label} service.", code="invalid_args")
            details = known.get(service)
            if isinstance(details, dict):
                return_response = "response" in cast("dict[str, object]", details)
        timeout = DEFAULT_CALL_TIMEOUT_S
        if self._deadline is not None:
            timeout = min(timeout, self._deadline - asyncio.get_running_loop().time())
            if timeout <= 0:
                raise ToolError("Home Assistant took too long.", code="timeout")
        log = get_logger(__name__).bind(turn_id=principal.turn_id, service=label)
        try:
            await conn.call_service(
                domain,
                service,
                data,
                {"entity_id": sorted(entity_ids)},
                return_response=return_response,
                timeout=timeout,
            )
        except HACommandError as exc:
            log.warning("ha.call_service.failed", code=exc.code)
            return ServiceResult(ok=False, detail=exc.code)
        except HAUnavailableError as exc:
            raise ToolError(
                "I can't reach the home right now.", code="unavailable", retryable=True
            ) from exc
        except HAWebSocketError as exc:
            raise ToolError("Home Assistant took too long.", code="timeout") from exc
        log.info("ha.call_service", targets=len(entity_ids))
        return ServiceResult(ok=True)


__all__ = ["TARGETING_KEYS", "HARuntime", "PolicyHAClient", "ServiceCatalog"]
