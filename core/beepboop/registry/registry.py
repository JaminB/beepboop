"""The one tool registry and its ``dispatch`` (docs/ARCHITECTURE.md §6, MCP §1).

``dispatch`` is the ``authorize → execute → audit`` chain every door uses:

1. look the tool up and validate ``args`` against its manifest schema;
2. ask the :class:`TargetExtractor` which HA entities the call touches;
3. ``policy.authorize`` (the only gate, CLAUDE.md hard rule 1);
4. ``deny`` → audit + :class:`PolicyDenied`; ``confirm`` → approvals row + audit +
   :class:`NeedsConfirmation`; ``allow`` → (consume the approval if any) run the handler
   with a deadline, taint the conversation when ``untrusted_output``, audit, return.

Front doors never call a handler themselves.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import jsonschema

from beepboop.log import get_logger
from beepboop.module.base import Module
from beepboop.module.context import ToolContext
from beepboop.module.types import HAClient, ToolError, ToolHandler, ToolResult
from beepboop.policy.approvals import ApprovalRecord, ApprovalService
from beepboop.policy.authorize import PolicyState, approval_bound, authorize
from beepboop.policy.digest import args_digest
from beepboop.policy.errors import NeedsConfirmation, PolicyDenied
from beepboop.policy.types import Approval, Decision, Principal, Risk, Target
from beepboop.registry.allowlist import BUILTIN_TOOLS
from beepboop.registry.manifest import Manifest, ToolSpec


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A manifest tool plus where it came from."""

    spec: ToolSpec
    module_id: str
    handler: ToolHandler

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def risk(self) -> Risk:
        return self.spec.risk


class TargetExtractor(Protocol):
    """Maps ``(tool, args)`` to the HA entities the call touches, before ``authorize``.

    The ``ha`` builder implements it for ``home.*``/``media.*`` through the resolver; the
    default returns no targets.
    """

    async def __call__(
        self, principal: Principal, tool: RegisteredTool, args: Mapping[str, Any]
    ) -> list[Target]: ...


async def no_targets(
    principal: Principal, tool: RegisteredTool, args: Mapping[str, Any]
) -> list[Target]:
    return []


@runtime_checkable
class BindsTargets(Protocol):
    """An ``HAClient`` that can be narrowed to the targets ``dispatch`` authorized
    (``PolicyHAClient`` implements it; fakes need not)."""

    def with_authorized_targets(self, uuids: Iterable[str]) -> HAClient: ...


class ContextFactory(Protocol):
    """Builds the ``ToolContext`` for one call. The core service wires the real one."""

    def for_call(
        self,
        tool: RegisteredTool,
        principal: Principal,
        *,
        deadline: float | None,
        approval: Approval | None,
    ) -> ToolContext: ...


@dataclass(frozen=True, slots=True)
class ToolCallAudit:
    """What the registry records about one dispatch (→ ``tool_calls`` row)."""

    turn_id: str
    principal: Principal
    tool: str
    module_id: str
    args_redacted: dict[str, Any]
    targets: list[Target]
    decision: Decision
    result_status: str
    untrusted_output: bool
    duration_ms: int | None = None
    approval_id: str | None = None
    error: str | None = None


class AuditSink(Protocol):
    async def record(self, entry: ToolCallAudit) -> None: ...


class TaintTracker(Protocol):
    """Conversation taint (IDENTITY_AND_TRUST §3.2); the agent owns the conversation row."""

    async def taint(self, principal: Principal) -> None: ...


class ListAuditSink:
    def __init__(self) -> None:
        self.entries: list[ToolCallAudit] = []

    async def record(self, entry: ToolCallAudit) -> None:
        self.entries.append(entry)


class NoTaint:
    async def taint(self, principal: Principal) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    tool: RegisteredTool
    result: ToolResult
    decision: Decision
    duration_ms: int
    tainted: bool


Summariser = Callable[[RegisteredTool, Mapping[str, Any], list[Target]], str]


def default_summary(tool: RegisteredTool, args: Mapping[str, Any], targets: list[Target]) -> str:
    """Plain-words approval summary from tool and args, never from the LLM (§8.3)."""
    verb = tool.spec.verb.replace("_", " ")
    what = ", ".join(str(v) for v in args.values() if isinstance(v, str | int | float))[:80]
    return f"{verb} {what}".strip()


def redact_args(spec: ToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    """``x-sensitive`` properties become ``"[redacted]"``; long strings are truncated (§11)."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in spec.sensitive_args:
            out[k] = "[redacted]"
        elif isinstance(v, str) and len(v) > 500:
            out[k] = v[:500] + "…"
        else:
            out[k] = v
    return out


class ToolNotFoundError(KeyError):
    pass


class ArgsInvalidError(ValueError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.detail = message


@dataclass
class Registry:
    """Core tools and every enabled module's tools, with their manifest metadata."""

    policy_state: Callable[[], PolicyState]
    approvals: ApprovalService
    context_factory: ContextFactory
    audit: AuditSink = field(default_factory=ListAuditSink)
    taint: TaintTracker = field(default_factory=NoTaint)
    targets: TargetExtractor = no_targets
    summarise: Summariser = default_summary
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _tools: dict[str, RegisteredTool] = field(default_factory=dict[str, RegisteredTool])
    _modules: dict[str, tuple[Manifest, Module]] = field(
        default_factory=dict[str, tuple[Manifest, Module]]
    )
    _agent_md: dict[str, str] = field(default_factory=dict[str, str])
    _setup_md: dict[str, str] = field(default_factory=dict[str, str])
    _seq: dict[str, int] = field(default_factory=dict[str, int])

    # -------------------------------------------------------------- registration --

    def register_module(
        self, manifest: Manifest, module: Module, agent_md: str = "", setup_md: str = ""
    ) -> None:
        """Register every manifest tool with its ``@tool`` handler (names must match, L2).
        ``agent_md`` is served by ``read_guide``; ``setup_md`` by the Integrations page."""
        handlers = module.tools()
        for spec in manifest.tools:
            handler = handlers.get(spec.name)
            if handler is None:
                msg = f"{manifest.id}: no @tool handler for {spec.name}"
                raise ToolNotFoundError(msg)
            if spec.name in self._tools:
                msg = f"tool {spec.name} already registered by {self._tools[spec.name].module_id}"
                raise ValueError(msg)
            self._tools[spec.name] = RegisteredTool(spec, manifest.id, handler)
        self._modules[manifest.id] = (manifest, module)
        self._agent_md[manifest.id] = agent_md
        self._setup_md[manifest.id] = setup_md

    def register_builtin(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name not in BUILTIN_TOOLS:
            msg = f"{spec.name} is not a registry built-in"
            raise ValueError(msg)
        self._tools[spec.name] = RegisteredTool(spec, "core", handler)

    def unregister_module(self, module_id: str) -> None:
        for name in [n for n, t in self._tools.items() if t.module_id == module_id]:
            del self._tools[name]
        self._modules.pop(module_id, None)
        self._agent_md.pop(module_id, None)
        self._setup_md.pop(module_id, None)

    # ------------------------------------------------------------------- lookup --

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name) from None

    def tools(self) -> list[RegisteredTool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def modules(self) -> list[tuple[Manifest, Module]]:
        return [self._modules[k] for k in sorted(self._modules)]

    def manifest(self, module_id: str) -> Manifest | None:
        entry = self._modules.get(module_id)
        return None if entry is None else entry[0]

    def guide(self, module_id: str) -> str | None:
        return self._agent_md.get(module_id)

    def setup_guide(self, module_id: str) -> str | None:
        """The module's human-facing ``SETUP.md`` (WEB_UI §5.6 "Setup guide" fold)."""
        return self._setup_md.get(module_id)

    def always_loaded(self) -> list[RegisteredTool]:
        return [t for t in self.tools() if not t.spec.deferred or t.module_id == "core"]

    def callable_by(
        self, principal: Principal, now: datetime | None = None
    ) -> list[RegisteredTool]:
        """Tools whose own grant check would pass for ``principal`` (no targets)."""
        state = self.policy_state()
        when = now or self.clock()
        out: list[RegisteredTool] = []
        for t in self.tools():
            d = authorize(state, principal, t, {}, [], when)
            if d.outcome != "deny" or d.reason in ("identity", "no_factor"):
                out.append(t)
        return out

    # ----------------------------------------------------------------- dispatch --

    def _next_seq(self, turn_id: str) -> int:
        self._seq[turn_id] = self._seq.get(turn_id, 0) + 1
        return self._seq[turn_id]

    async def dispatch(
        self,
        principal: Principal,
        tool_name: str,
        args: Mapping[str, Any],
        *,
        approval: ApprovalRecord | None = None,
        dry_run: bool = False,
    ) -> DispatchResult:
        """The ``authorize → execute → audit`` chain for one call. Raises
        :class:`PolicyDenied`, :class:`NeedsConfirmation`, :class:`ArgsInvalidError`,
        :class:`ToolNotFoundError` or :class:`ToolError`."""
        log = get_logger(__name__).bind(turn_id=principal.turn_id, tool=tool_name)
        tool = self.get(tool_name)
        try:
            jsonschema.validate(dict(args), tool.spec.args)
        except jsonschema.ValidationError as exc:
            raise ArgsInvalidError(tool_name, exc.message) from exc

        targets = await self.targets(principal, tool, args)
        now = self.clock()
        state = self.policy_state()
        decision = authorize(
            state,
            principal,
            tool,
            args,
            targets,
            now,
            approval=None if approval is None else approval.to_policy(),
        )
        redacted = redact_args(tool.spec, args)
        log.info(
            "policy.decision",
            decision=decision.outcome,
            reason=decision.reason,
            effective_risk=decision.effective_risk,
        )

        if decision.outcome == "deny":
            await self.audit.record(
                ToolCallAudit(
                    principal.turn_id,
                    principal,
                    tool.name,
                    tool.module_id,
                    redacted,
                    targets,
                    decision,
                    "denied",
                    tool.spec.untrusted_output,
                    approval_id=None if approval is None else approval.id,
                )
            )
            raise PolicyDenied(
                decision.reason or "no_grant",
                decision.effective_risk,
                resource=decision.resource,
                tool=tool.name,
            )

        if state.rate_limiter is not None:
            state.rate_limiter.record(principal.profile_id, principal.device, tool.name, now)

        if decision.outcome == "confirm":
            record, nonce = await self.approvals.create(
                principal,
                tool.name,
                args,
                (t.uuid for t in targets),
                self.summarise(tool, args, targets),
                now,
            )
            await self.audit.record(
                ToolCallAudit(
                    principal.turn_id,
                    principal,
                    tool.name,
                    tool.module_id,
                    redacted,
                    targets,
                    decision,
                    "denied",
                    tool.spec.untrusted_output,
                    approval_id=record.id,
                )
            )
            raise NeedsConfirmation(record, decision.factors, nonce)

        # allow
        if approval is not None:
            # An approval handed to a call it was not minted for is never consumed, even when
            # the call needed no factor (§8.2: same profile, same request, single use).
            bound = approval_bound(
                approval.to_policy(),
                principal,
                tool.name,
                args_digest(tool.name, args, (t.uuid for t in targets)),
            )
            if not bound or (
                not dry_run and await self.approvals.consume(approval.id, now) is None
            ):
                # IDENTITY_AND_TRUST §11: every approval execution is audited, failed ones too.
                await self.audit.record(
                    ToolCallAudit(
                        principal.turn_id,
                        principal,
                        tool.name,
                        tool.module_id,
                        redacted,
                        targets,
                        Decision("deny", decision.effective_risk, reason="approval_invalid"),
                        "denied",
                        tool.spec.untrusted_output,
                        approval_id=approval.id,
                    )
                )
                raise PolicyDenied("approval_invalid", decision.effective_risk, tool=tool.name)

        deadline = asyncio.get_running_loop().time() + tool.spec.timeout_ms / 1000
        ctx = self.context_factory.for_call(
            tool,
            principal,
            deadline=deadline,
            approval=None if approval is None else approval.to_policy(),
        )
        if dry_run and not ctx.dry_run:
            # MODULES §12.3: a dry-run call records intended effects instead of acting.
            ctx = replace(ctx, dry_run=True)
        if isinstance(ctx.ha, BindsTargets):
            # HOME_ASSISTANT §8: the policy-wrapped client only acts on the entities this
            # dispatch authorized, so a handler cannot widen an approval to other targets.
            ctx = replace(ctx, ha=ctx.ha.with_authorized_targets([t.uuid for t in targets]))
        started = time.monotonic()
        status = "ok"
        error: str | None = None
        try:
            raw = await asyncio.wait_for(
                tool.handler(ctx, **dict(args)), timeout=tool.spec.timeout_ms / 1000
            )
            result = raw if isinstance(raw, ToolResult) else ToolResult(structured=dict(raw))
        except TimeoutError:
            status, error = "timeout", "timeout"
            result = ToolResult({"error": "tool_failed", "code": "timeout", "speak": ""})
        except ToolError as exc:
            status, error = "error", exc.code
            result = ToolResult(
                {"error": exc.message, "code": exc.code, "speak": ""},
                text=f"{exc.code}: {exc.message}",
            )
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status, error = "error", "other"
            log.exception("tool.failed", module_id=tool.module_id, error=type(exc).__name__)
            result = ToolResult({"error": "tool_failed", "code": "other", "speak": ""})
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if dry_run and status == "ok":
                status = "dry_run"
            await self.audit.record(
                ToolCallAudit(
                    principal.turn_id,
                    principal,
                    tool.name,
                    tool.module_id,
                    redacted,
                    targets,
                    decision,
                    status,
                    tool.spec.untrusted_output,
                    duration_ms,
                    None if approval is None else approval.id,
                    error,
                )
            )

        tainted = False
        if tool.spec.untrusted_output and status in ("ok", "dry_run"):
            await self.taint.taint(principal)
            tainted = True
        return DispatchResult(tool, result, decision, duration_ms, tainted)


type Dispatcher = Callable[..., Awaitable[DispatchResult]]

__all__ = [
    "ArgsInvalidError",
    "AuditSink",
    "BindsTargets",
    "ContextFactory",
    "DispatchResult",
    "Dispatcher",
    "ListAuditSink",
    "NoTaint",
    "RegisteredTool",
    "Registry",
    "TaintTracker",
    "TargetExtractor",
    "ToolCallAudit",
    "ToolNotFoundError",
    "default_summary",
    "no_targets",
    "redact_args",
]
