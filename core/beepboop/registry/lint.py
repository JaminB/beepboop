"""``beepboop module check``: lint rules L1-L18 (docs/MODULES.md §4.6) and token budgets."""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import jsonschema

from beepboop import tokens
from beepboop.module.base import handler_parameters
from beepboop.registry.allowlist import (
    CORE_TOOL_MAX_TIMEOUT_MS,
    allowed_namespaces,
    may_be_core,
)
from beepboop.registry.loader import import_module_file, missing_files
from beepboop.registry.manifest import Manifest, ManifestError, ToolSpec, load_manifest
from beepboop.testing.evals_model import EvalsError, EvalsFile, load_evals

Level = Literal["error", "warning"]

BANNED_IMPORTS = ("mcp", "homeassistant", "boto3", "botocore", "anthropic", "openai", "websockets")
BANNED_IMPORT_PREFIXES = ("beepboop.ha",)
BANNED_CALLS = frozenset({"open", "socket", "subprocess", "sqlite3"})
AGENT_SECTIONS = [
    "When to use",
    "When NOT to use",
    "Arguments",
    "Examples",
    "Failures",
    "Speaking the result",
]
SETTINGS_X_KEYS = frozenset({"x-entity", "x-advanced", "x-per-profile", "x-order", "x-enum-labels"})
SETTINGS_FORBIDDEN = frozenset({"oneOf", "anyOf", "allOf", "$ref", "patternProperties"})
DOTTED_TOOL = re.compile(r"`([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)`")

# L4 budgets (tokens).
TOOL_DEF_BUDGET = 400
CARD_BUDGET = 60
AGENT_BUDGET = 2000
SLACK = 1.10
WARN_AT = 0.80


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    level: Level
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.level}: {self.message}"


@dataclass(frozen=True, slots=True)
class LintReport:
    module_dir: Path
    findings: tuple[Finding, ...]
    manifest: Manifest | None = None

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


def _words(text: str) -> int:
    return len(text.split())


def _budget(findings: list[Finding], rule: str, what: str, count: int, budget: int) -> None:
    if count > budget * SLACK:
        findings.append(Finding(rule, "error", f"{what} is {count} tokens; budget {budget}"))
    elif count > budget * WARN_AT:
        findings.append(Finding(rule, "warning", f"{what} is {count} tokens; budget {budget}"))


def _tool_def_text(tool: ToolSpec) -> str:
    return json.dumps(
        {"name": tool.name, "description": tool.description, "input_schema": tool.args},
        sort_keys=True,
    )


def lint_manifest(manifest: Manifest, module_dir: Path, findings: list[Finding]) -> None:
    namespaces = allowed_namespaces(manifest.id)
    if _words(manifest.summary) > 20:
        findings.append(Finding("L3", "error", "summary must be <= 20 words"))
    if not manifest.summary.rstrip().endswith("."):
        findings.append(Finding("L3", "warning", "summary should end with a period"))
    _budget(
        findings,
        "L4",
        "module card",
        tokens.estimate(f"- {manifest.id} — {manifest.summary} [{', '.join(manifest.tool_names)}]"),
        CARD_BUDGET,
    )
    for tool in manifest.tools:
        if tool.namespace not in namespaces:
            findings.append(
                Finding(
                    "L10",
                    "error",
                    f"{tool.name}: namespace {tool.namespace!r} not allowed for module"
                    f" {manifest.id!r}",
                )
            )
        if not tool.deferred and not may_be_core(manifest.id, tool.name):
            findings.append(
                Finding(
                    "L10", "error", f"{tool.name}: deferred: false is only for the core allowlist"
                )
            )
        if not tool.deferred and tool.timeout_ms > CORE_TOOL_MAX_TIMEOUT_MS:
            findings.append(
                Finding(
                    "L10",
                    "error",
                    f"{tool.name}: core tools may not exceed {CORE_TOOL_MAX_TIMEOUT_MS} ms",
                )
            )
        if _words(tool.description) > 80:
            findings.append(Finding("L3", "error", f"{tool.name}: description must be ≤ 80 words"))
        props: dict[str, Any] = tool.args.get("properties", {})
        for arg, schema in props.items():
            desc = _description(schema)
            if not isinstance(desc, str) or not desc:
                findings.append(
                    Finding("L3", "error", f"{tool.name}.{arg}: every arg needs a description")
                )
            elif _words(desc) > 25:
                findings.append(
                    Finding("L3", "error", f"{tool.name}.{arg}: arg description must be ≤ 25 words")
                )
        _budget(
            findings,
            "L4",
            f"tool definition {tool.name}",
            tokens.estimate(_tool_def_text(tool)),
            TOOL_DEF_BUDGET,
        )
        if not tool.examples:
            findings.append(Finding("L7", "warning", f"{tool.name}: no examples"))
        for ex in tool.examples:
            try:
                jsonschema.validate(ex.args, tool.args)
            except jsonschema.ValidationError as exc:
                findings.append(
                    Finding(
                        "L7",
                        "error",
                        f"{tool.name}: example {ex.utterance!r} args invalid: {exc.message}",
                    )
                )
        _lint_description_prose(tool, findings)
    _lint_settings(manifest.settings, findings)
    if manifest.prompt_block is not None and manifest.category != "persona":
        findings.append(Finding("L18", "error", "prompt_block only with category: persona"))


def _description(schema: object) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = cast(dict[str, object], schema).get("description")
    return value if isinstance(value, str) else None


def _lint_description_prose(tool: ToolSpec, findings: list[Finding]) -> None:
    d = tool.description.strip()
    first = d.split()[0].lower() if d else ""
    if first.endswith(("ing", "ion")) or first in {"this", "the", "a", "an", "tool"}:
        findings.append(
            Finding("L14", "warning", f"{tool.name}: description should start with a verb")
        )
    if "this tool" in d.lower() or "http://" in d or "https://" in d or "**" in d or "`" in d:
        findings.append(
            Finding(
                "L14", "warning", f"{tool.name}: no markdown, URLs or 'this tool' in descriptions"
            )
        )


def _lint_settings(schema: dict[str, Any], findings: list[Finding], depth: int = 0) -> None:
    for key in schema:
        if key in SETTINGS_FORBIDDEN:
            findings.append(Finding("L12", "error", f"settings use unsupported {key}"))
        if key.startswith("x-") and key not in SETTINGS_X_KEYS:
            findings.append(Finding("L12", "error", f"settings use unknown keyword {key}"))
    if schema.get("format") == "password":
        findings.append(
            Finding("L11", "error", "settings may not use format: password; use secrets")
        )
    props = schema.get("properties")
    if isinstance(props, dict):
        for sub in props.values():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(sub, dict):
                if sub.get("type") == "object" and depth >= 1:  # pyright: ignore[reportUnknownMemberType]
                    findings.append(
                        Finding("L12", "error", "settings objects may nest only one level")
                    )
                _lint_settings(sub, findings, depth + 1)  # pyright: ignore[reportUnknownArgumentType]


def lint_module_py(module_dir: Path, manifest: Manifest, findings: list[Finding]) -> None:
    path = module_dir / "module.py"
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(
            Finding("L2", "error", f"module.py line {exc.lineno}: syntax error: {exc.msg}")
        )
        return
    uses_http = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import(alias.name, findings)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            _check_import(node.module, findings)
            for alias in node.names:
                _check_import(f"{node.module}.{alias.name}", findings)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else (fn.attr if isinstance(fn, ast.Attribute) else "")
            )
            if isinstance(fn, ast.Name) and name in BANNED_CALLS:
                findings.append(Finding("L9", "error", f"module.py calls {name}()"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "http"
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
        ):
            uses_http = True
    if uses_http and not manifest.egress:
        findings.append(
            Finding("L18", "error", "module.py uses ctx.http but the manifest has no egress entry")
        )
    # L2: handlers ↔ manifest tools.
    try:
        klass = import_module_file(module_dir, name=manifest.id)
    except Exception as exc:
        findings.append(Finding("L2", "error", f"module.py failed to import: {exc}"))
        return
    handlers = klass().tools()
    for spec in manifest.tools:
        handler = handlers.get(spec.name)
        if handler is None:
            findings.append(Finding("L2", "error", f"no @tool handler for {spec.name}"))
            continue
        _check_signature(spec, handler, findings)
    for name in handlers:
        if manifest.tool(name) is None:
            findings.append(Finding("L2", "error", f"@tool({name!r}) has no manifest entry"))


def _check_signature(spec: ToolSpec, handler: Any, findings: list[Finding]) -> None:
    try:
        params = handler_parameters(handler)
    except ValueError as exc:
        findings.append(Finding("L2", "error", f"{spec.name}: {exc}"))
        return
    props: dict[str, Any] = spec.args.get("properties", {})
    required = set(spec.args.get("required", []))
    if set(params) != set(props):
        findings.append(
            Finding(
                "L2",
                "error",
                f"{spec.name}: handler args {sorted(params)} != manifest args {sorted(props)}",
            )
        )
    for name, p in params.items():
        import inspect

        expected = _json_types(props.get(name, {}))
        actual = _annotation_json_types(p.annotation)
        if expected and actual and not (expected & actual):
            findings.append(
                Finding(
                    "L2",
                    "error",
                    f"{spec.name}.{name}: handler type {sorted(actual)} does not match"
                    f" manifest type {sorted(expected)}",
                )
            )
        has_default = p.default is not inspect.Parameter.empty
        if name in required and has_default:
            findings.append(
                Finding("L2", "warning", f"{spec.name}.{name}: required arg has a default")
            )
        if name in props and name not in required and not has_default:
            findings.append(
                Finding("L2", "error", f"{spec.name}.{name}: optional arg needs a default")
            )


_ANNOTATION_TYPES: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "Mapping": "object",
    "Sequence": "array",
}


def _json_types(prop: Mapping[str, Any]) -> set[str]:
    """The JSON types a manifest property allows (``number`` also admits ``integer``)."""
    raw = prop.get("type")
    names: set[str] = set()
    if isinstance(raw, list):
        names = {str(t) for t in cast(list[object], raw)}
    elif isinstance(raw, str):
        names = {raw}
    if "number" in names:
        names.add("integer")
    if "integer" in names:
        names.add("number")
    return names - {"null"}


def _annotation_json_types(annotation: object) -> set[str]:
    """The JSON types a handler annotation accepts; empty when unknown (``Any``, no
    annotation), which never fails the check."""
    if annotation is inspect.Parameter.empty:
        return set()
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    if not isinstance(text, str):  # pragma: no cover - defensive
        return set()
    out: set[str] = set()
    for part in re.split(r"\s*\|\s*", text):
        head = part.split("[", 1)[0].strip()
        if head in ("Any", "object"):
            return set()
        if head == "None":
            continue
        mapped = _ANNOTATION_TYPES.get(head)
        if mapped is None:
            return set()  # a custom type: not ours to judge
        out.add(mapped)
        if mapped == "number":
            out.add("integer")
        if mapped == "integer":
            out.add("number")
    return out


def _check_import(name: str, findings: list[Finding]) -> None:
    top = name.split(".", 1)[0]
    if (
        top in BANNED_IMPORTS
        or top in BANNED_CALLS
        or any(name == p or name.startswith(f"{p}.") for p in BANNED_IMPORT_PREFIXES)
    ):
        findings.append(Finding("L9", "error", f"module.py imports {name}"))


def lint_agent_md(
    module_dir: Path, manifest: Manifest, known_tools: set[str], findings: list[Finding]
) -> None:
    text = (module_dir / "AGENT.md").read_text(encoding="utf-8")
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.strip() != f"# {manifest.id}":
        findings.append(Finding("L5", "error", f"AGENT.md must start with '# {manifest.id}'"))
    headings = [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]
    order = [h for h in headings if h in AGENT_SECTIONS]
    if order != AGENT_SECTIONS:
        findings.append(
            Finding(
                "L5", "error", f"AGENT.md sections must be {AGENT_SECTIONS} in order; found {order}"
            )
        )
    for name in manifest.tool_names:
        if name not in text:
            findings.append(Finding("L6", "error", f"AGENT.md never mentions {name}"))
    own = set(manifest.tool_names)
    for dotted in set(DOTTED_TOOL.findall(text)):
        if dotted in own:
            continue
        if dotted.split(".", 1)[0] == manifest.id:
            findings.append(Finding("L6", "error", f"AGENT.md mentions unknown tool {dotted}"))
        elif known_tools and dotted not in known_tools:
            findings.append(
                Finding("L16", "warning", f"AGENT.md mentions {dotted}, not in the registry")
            )
    _budget(findings, "L4", "AGENT.md", tokens.estimate(text), AGENT_BUDGET)


def lint_setup_md(module_dir: Path, manifest: Manifest, findings: list[Finding]) -> None:
    text = (module_dir / "SETUP.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    needs_setup = bool(manifest.secrets) or bool(manifest.settings.get("properties"))
    if len(lines) < 10 and needs_setup:
        findings.append(
            Finding(
                "L15", "error", "SETUP.md must be >= 10 lines for a module with settings or secrets"
            )
        )


def lint_evals(module_dir: Path, manifest: Manifest, findings: list[Finding]) -> EvalsFile | None:
    try:
        evals = load_evals(module_dir)
    except EvalsError as exc:
        findings.append(Finding("L8", "error", f"evals.yaml: {exc}"))
        return None
    if evals.module != manifest.id:
        findings.append(Finding("L8", "error", f"evals.module {evals.module!r} != {manifest.id!r}"))
    per_tool = {t.name: 0 for t in manifest.tools}
    negatives = denials = 0
    for case in evals.cases:
        if case.expect.tool is None:
            negatives += 1
        elif case.expect.tool in per_tool:
            per_tool[case.expect.tool] += 1
        elif case.expect.tool.split(".", 1)[0] == manifest.id:
            findings.append(
                Finding("L8", "error", f"evals case {case.id}: unknown tool {case.expect.tool!r}")
            )
        if case.expect.denied:
            denials += 1
        if case.expect.args is not None and case.expect.tool in per_tool:
            spec = manifest.tool(case.expect.tool)
            if spec is not None:
                _validate_expect_args(spec, case.expect.args, case.id, findings)
    for name, n in per_tool.items():
        if n < 3:
            findings.append(Finding("L8", "error", f"evals: {name} has {n} cases; need >= 3"))
    if negatives < 1:
        findings.append(Finding("L8", "error", "evals: need >= 1 negative case (tool: null)"))
    if any(t.risk in ("personal", "critical") for t in manifest.tools) and denials < 1:
        findings.append(
            Finding("L8", "error", "evals: need >= 1 denial case for personal/critical tools")
        )
    return evals


def _is_matcher(value: object) -> bool:
    return isinstance(value, dict) and bool(
        {"$regex", "$any"} & set(cast(dict[str, object], value))
    )


def _validate_expect_args(
    spec: ToolSpec, args: dict[str, Any], case_id: str, findings: list[Finding]
) -> None:
    plain: dict[str, Any] = {k: v for k, v in args.items() if not _is_matcher(v)}
    schema = dict(spec.args)
    schema["required"] = [r for r in schema.get("required", []) if r in plain]
    try:
        jsonschema.validate(plain, schema)
    except jsonschema.ValidationError as exc:
        findings.append(
            Finding("L8", "error", f"evals case {case_id}: args invalid: {exc.message}")
        )


def lint_module(module_dir: Path, *, known_tools: set[str] | None = None) -> LintReport:
    """Run L1-L18 on one module folder (no evals execution; see ``beepboop.testing.run_evals``)."""
    module_dir = Path(module_dir)
    findings: list[Finding] = []
    missing = missing_files(module_dir)
    for name in missing:
        findings.append(Finding("L1", "error", f"missing {name}"))
    try:
        manifest = load_manifest(module_dir)
    except ManifestError as exc:
        findings.append(Finding("L1", "error", str(exc)))
        return LintReport(module_dir, tuple(findings))
    lint_manifest(manifest, module_dir, findings)
    if "module.py" not in missing:
        lint_module_py(module_dir, manifest, findings)
    if "AGENT.md" not in missing:
        lint_agent_md(module_dir, manifest, known_tools or set(), findings)
    if "SETUP.md" not in missing:
        lint_setup_md(module_dir, manifest, findings)
    if "evals.yaml" not in missing:
        lint_evals(module_dir, manifest, findings)
    return LintReport(module_dir, tuple(findings), manifest)


__all__ = ["AGENT_SECTIONS", "Finding", "LintReport", "lint_module"]
