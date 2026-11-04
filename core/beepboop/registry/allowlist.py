"""The core allowlist (docs/MODULES.md §4.7): changing this needs an ADR.

Three in-repo modules own the always-loaded intent namespaces and may set
``deferred: false``. ``find_tools`` and ``read_guide`` are registry built-ins.
"""

from __future__ import annotations

from types import MappingProxyType

#: module id → namespaces it may use instead of its own id (§4.5).
NAMESPACE_ALLOWLIST: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        "ha": frozenset({"home", "media"}),
        "timers": frozenset({"timer"}),
        "lists": frozenset({"list"}),
    }
)

#: Tools that may be ``deferred: false`` (the 10 module tools of the 12 always-loaded).
ALWAYS_LOADED_MODULE_TOOLS: frozenset[str] = frozenset(
    {
        "home.turn_on",
        "home.turn_off",
        "home.set",
        "home.state",
        "media.control",
        "timer.set",
        "timer.list",
        "timer.cancel",
        "list.add",
        "list.read",
    }
)

#: Registry built-ins, not module tools.
BUILTIN_TOOLS: frozenset[str] = frozenset({"find_tools", "read_guide"})

#: The 12 always-loaded tools (ARCHITECTURE §6).
ALWAYS_LOADED: frozenset[str] = ALWAYS_LOADED_MODULE_TOOLS | BUILTIN_TOOLS

#: Core tools may not exceed this handler deadline (§4.5).
CORE_TOOL_MAX_TIMEOUT_MS = 2000


def allowed_namespaces(module_id: str) -> frozenset[str]:
    """Namespaces ``module_id`` may use for tool names: its own id plus any allowlisted."""
    return frozenset({module_id}) | NAMESPACE_ALLOWLIST.get(module_id, frozenset())


def may_be_core(module_id: str, tool_name: str) -> bool:
    """True if this module may declare ``tool_name`` as ``deferred: false``."""
    return module_id in NAMESPACE_ALLOWLIST and tool_name in ALWAYS_LOADED_MODULE_TOOLS


__all__ = [
    "ALWAYS_LOADED",
    "ALWAYS_LOADED_MODULE_TOOLS",
    "BUILTIN_TOOLS",
    "CORE_TOOL_MAX_TIMEOUT_MS",
    "NAMESPACE_ALLOWLIST",
    "allowed_namespaces",
    "may_be_core",
]
