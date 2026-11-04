"""The tool registry: manifests, discovery, dispatch, resolver, ``find_tools``/``read_guide``, lint.

docs/MODULES.md §4 (manifest, lint), ARCHITECTURE §6 (one registry, two doors), MCP §6-§7
(deferral, cards), HOME_ASSISTANT §6.3 (resolver).
"""

from beepboop.registry.allowlist import (
    ALWAYS_LOADED,
    ALWAYS_LOADED_MODULE_TOOLS,
    BUILTIN_TOOLS,
    NAMESPACE_ALLOWLIST,
    allowed_namespaces,
    may_be_core,
)
from beepboop.registry.builtins import (
    FIND_TOOLS_SPEC,
    READ_GUIDE_SPEC,
    make_find_tools,
    make_read_guide,
)
from beepboop.registry.find_tools import FoundTool, ToolIndex, module_card, module_cards
from beepboop.registry.lint import Finding, LintReport, lint_module
from beepboop.registry.loader import (
    REQUIRED_FILES,
    DiscoveredModule,
    ModuleLoadError,
    discover,
    import_module_file,
)
from beepboop.registry.manifest import (
    SCHEMA_PATH,
    Manifest,
    ManifestError,
    ToolSpec,
    load_manifest,
    manifest_schema,
    parse_manifest,
    write_schema,
)
from beepboop.registry.registry import (
    ArgsInvalidError,
    AuditSink,
    ContextFactory,
    DispatchResult,
    ListAuditSink,
    NoTaint,
    RegisteredTool,
    Registry,
    TaintTracker,
    TargetExtractor,
    ToolCallAudit,
    ToolNotFoundError,
    no_targets,
    redact_args,
)
from beepboop.registry.resolver import (
    Area,
    EntityIndex,
    Floor,
    Resolution,
    StaticEntityIndex,
    TargetQuery,
    resolve,
)

__all__ = [
    "ALWAYS_LOADED",
    "ALWAYS_LOADED_MODULE_TOOLS",
    "BUILTIN_TOOLS",
    "FIND_TOOLS_SPEC",
    "NAMESPACE_ALLOWLIST",
    "READ_GUIDE_SPEC",
    "REQUIRED_FILES",
    "SCHEMA_PATH",
    "Area",
    "ArgsInvalidError",
    "AuditSink",
    "ContextFactory",
    "DiscoveredModule",
    "DispatchResult",
    "EntityIndex",
    "Finding",
    "Floor",
    "FoundTool",
    "LintReport",
    "ListAuditSink",
    "Manifest",
    "ManifestError",
    "ModuleLoadError",
    "NoTaint",
    "RegisteredTool",
    "Registry",
    "Resolution",
    "StaticEntityIndex",
    "TaintTracker",
    "TargetExtractor",
    "TargetQuery",
    "ToolCallAudit",
    "ToolIndex",
    "ToolNotFoundError",
    "ToolSpec",
    "allowed_namespaces",
    "discover",
    "import_module_file",
    "lint_module",
    "load_manifest",
    "make_find_tools",
    "make_read_guide",
    "manifest_schema",
    "may_be_core",
    "module_card",
    "module_cards",
    "no_targets",
    "parse_manifest",
    "redact_args",
    "resolve",
    "write_schema",
]
