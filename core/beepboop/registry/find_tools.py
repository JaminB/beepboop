"""``find_tools`` and ``read_guide``: deferral (docs/MCP.md §6) and the module-card renderer
(MCP §7). BM25 over one document per tool; about sixty lines, no vector DB.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from beepboop.module.types import HealthStatus
from beepboop.registry.manifest import Manifest, ToolSpec

_TOKEN = re.compile(r"[a-z0-9]+")
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace(".", " ").replace("_", " "))


def agent_md_terms(agent_md: str) -> str:
    """Headings and 'When to use' bullets (MCP §6): the parts worth indexing."""
    out: list[str] = []
    in_when = False
    for line in agent_md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            in_when = s.lower().lstrip("#").strip() == "when to use"
        elif in_when and s.startswith("-"):
            out.append(s.lstrip("- ").strip())
    return " ".join(out)


def tool_document(manifest: Manifest, tool: ToolSpec, agent_md: str = "") -> str:
    props: object = tool.args.get("properties", {})
    items: dict[str, Any] = dict(cast(dict[str, Any], props)) if isinstance(props, dict) else {}
    parts: list[str] = []
    for k, v in items.items():
        desc = cast(dict[str, Any], v).get("description", "") if isinstance(v, dict) else ""
        parts.append(f"{k} {desc}")
    arg_text = " ".join(parts)
    return " ".join(
        [
            tool.name,
            tool.description,
            arg_text,
            manifest.summary,
            " ".join(manifest.keywords),
            agent_md_terms(agent_md),
        ]
    )


@dataclass(frozen=True, slots=True)
class FoundTool:
    name: str
    module_id: str
    description: str
    score: float


@dataclass
class ToolIndex:
    """BM25 index; rebuild (``ToolIndex.build``) when modules load or AGENT.md changes."""

    docs: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    module_of: dict[str, str] = field(default_factory=dict[str, str])
    description_of: dict[str, str] = field(default_factory=dict[str, str])
    _df: Counter[str] = field(default_factory=Counter[str])
    _avgdl: float = 0.0

    @classmethod
    def build(cls, entries: Iterable[tuple[Manifest, ToolSpec, str]]) -> ToolIndex:
        index = cls()
        for manifest, tool, agent_md in entries:
            tokens = tokenize(tool_document(manifest, tool, agent_md))
            index.docs[tool.name] = tokens
            index.module_of[tool.name] = manifest.id
            index.description_of[tool.name] = tool.description
            index._df.update(set(tokens))
        if index.docs:
            index._avgdl = sum(len(t) for t in index.docs.values()) / len(index.docs)
        return index

    def _idf(self, term: str) -> float:
        n = len(self.docs)
        df = self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Sequence[str], name: str) -> float:
        doc = self.docs[name]
        tf = Counter(doc)
        dl = len(doc)
        total = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if not f:
                continue
            denom = f + K1 * (1 - B + B * dl / (self._avgdl or 1))
            total += self._idf(term) * f * (K1 + 1) / denom
        return total

    def search(
        self, query: str, *, allowed: Iterable[str] | None = None, limit: int = 5
    ) -> list[FoundTool]:
        """Top ``limit`` tools for ``query``, restricted to ``allowed`` tool names."""
        q = tokenize(query)
        names = set(self.docs) if allowed is None else set(allowed) & set(self.docs)
        scored = [(n, self.score(q, n)) for n in names]
        scored = [(n, s) for n, s in scored if s > 0]
        scored.sort(key=lambda ns: (-ns[1], ns[0]))
        return [
            FoundTool(n, self.module_of[n], self.description_of[n], round(s, 4))
            for n, s in scored[:limit]
        ]


# ------------------------------------------------------------ module cards --


def module_card(manifest: Manifest, health: HealthStatus = "ok") -> str:
    """One line per module (MCP §7): ``- id — summary [tool, tool]`` (+ needs reconnecting)."""
    tools = ", ".join(t.name for t in manifest.tools if t.deferred)
    suffix = " (needs reconnecting)" if health in ("reauth", "error") else ""
    return f"- {manifest.id} — {manifest.summary} [{tools}]{suffix}"


def module_cards(entries: Sequence[tuple[Manifest, HealthStatus]], *, profile_name: str) -> str:
    """The block for one profile, sorted by module id for cache stability."""
    lines = [f"Connected for {profile_name} (call find_tools to load these):"]
    for manifest, health in sorted(entries, key=lambda mh: mh[0].id):
        if any(t.deferred for t in manifest.tools) or manifest.prompt_block is not None:
            lines.append(module_card(manifest, health))
    return "\n".join(lines)


__all__ = [
    "FoundTool",
    "ToolIndex",
    "agent_md_terms",
    "module_card",
    "module_cards",
    "tokenize",
    "tool_document",
]
