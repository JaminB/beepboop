"""Enforce CLAUDE.md hard rule 2: modules never import MCP, HA or LLM SDKs.

Scans every ``*.py`` under ``modules/`` and fails on an import of ``mcp``,
``homeassistant``, ``boto3``, ``botocore``, ``openai``, ``anthropic`` or
``beepboop.ha`` (including ``from beepboop import ha``). It parses imports
with :mod:`ast` instead of grepping text, so comments and docstrings that
merely mention a name do not trip it. A module gets everything it needs from
``ToolContext`` (MODULES §5.3).

Usage::

    uv run python tools/ci/import_ban.py              # scans ./modules
    uv run python tools/ci/import_ban.py path/to/dir

Exit status is 1 on any banned import, 0 otherwise (including when
``modules/`` does not exist yet).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Top-level packages a module may never import.
BANNED_TOP_LEVEL: frozenset[str] = frozenset(
    {"mcp", "homeassistant", "boto3", "botocore", "openai", "anthropic"}
)
#: Dotted core packages a module may never import (it gets ``ctx.ha`` instead).
BANNED_PREFIXES: tuple[str, ...] = ("beepboop.ha",)


def is_banned(module: str) -> bool:
    """True if importing the dotted name ``module`` violates hard rule 2."""
    if module.split(".", 1)[0] in BANNED_TOP_LEVEL:
        return True
    return any(module == p or module.startswith(f"{p}.") for p in BANNED_PREFIXES)


def banned_imports(source: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line, dotted_name)`` for each banned import in ``source``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_banned(alias.name):
                    yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if is_banned(node.module):
                yield node.lineno, node.module
            for alias in node.names:
                if is_banned(f"{node.module}.{alias.name}"):
                    yield node.lineno, f"{node.module}.{alias.name}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description="CLAUDE.md hard rule 2 import ban")
    parser.add_argument("root", nargs="?", type=Path, default=REPO_ROOT / "modules")
    args = parser.parse_args(argv)
    root: Path = args.root
    if not root.exists():
        print(f"import_ban: {root} does not exist; nothing to check")
        return 0
    hits = 0
    for path in sorted(root.rglob("*.py")):
        for line, name in banned_imports(path.read_text(encoding="utf-8")):
            print(f"{path.as_posix()}:{line}: banned import {name} (CLAUDE.md hard rule 2)")
            hits += 1
    print(f"import_ban: {hits} banned imports")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
