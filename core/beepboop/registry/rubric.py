"""``module check --rubric``: the computed rows of the AGENT.md rubric (MODULES §10.4).

R1, R4, R5, R7 are computed here; R8 comes from the evals run; R2, R3, R6 are reviewer rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from beepboop import tokens
from beepboop.registry.manifest import Manifest

REQUIRED_FAILURE_CODES = ("reauth", "pending_approval")


@dataclass(frozen=True, slots=True)
class RubricScore:
    r1_triggers: int
    r4_examples: int
    r5_failures: int
    r7_density: int

    @property
    def auto_total(self) -> int:
        return self.r1_triggers + self.r4_examples + self.r5_failures + self.r7_density


def _section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def score(agent_md: str, manifest: Manifest) -> RubricScore:
    when = _section(agent_md, "When to use")
    phrasings_per_tool: list[int] = []
    for tool in manifest.tools:
        bullets = [ln for ln in when.splitlines() if tool.name in ln]
        phrasings_per_tool.append(sum(len(re.findall(r'"[^"]+"', ln)) for ln in bullets))
    least = min(phrasings_per_tool, default=0)
    r1 = 2 if least >= 5 else 1 if least >= 2 else 0

    examples = _section(agent_md, "Examples")
    numbered = len(re.findall(r"^\d+\.", examples, re.M))
    has_json = "{" in examples and "}" in examples
    ambiguous = "ambiguous" in examples.lower()
    follow_up = "follow-up" in examples.lower() or "follow up" in examples.lower()
    r4 = (
        0
        if numbered < 3 or not has_json
        else 2
        if (ambiguous and follow_up and numbered <= 7)
        else 1
    )

    failures = _section(agent_md, "Failures").lower()
    r5 = (
        2
        if all(code in failures for code in REQUIRED_FAILURE_CODES)
        else 1
        if failures.strip()
        else 0
    )

    n = tokens.estimate(agent_md)
    r7 = 0 if n > 2000 else 2 if n <= 1400 else 1
    return RubricScore(r1, r4, r5, r7)


#: MODULES §10.4: eight rows of two points; R2, R3 and R6 need a reviewer, R8 the evals.
RUBRIC_TOTAL = 16
RUBRIC_BAR = 12


def auto_rubric(module_dir: Path, manifest: Manifest, *, evals_ok: bool | None = None) -> str:
    """One line a module author can act on: the computed total so far, the bar, and which
    rows are still pending (``R8`` once the evals have run)."""
    text = (module_dir / "AGENT.md").read_text(encoding="utf-8")
    s = score(text, manifest)
    total = s.auto_total
    pending = ["R2", "R3", "R6"]
    if evals_ok is None:
        pending.append("R8")
    elif evals_ok:
        total += 2
    return (
        f"rubric: {total}/{RUBRIC_TOTAL} so far (bar {RUBRIC_BAR}): R1={s.r1_triggers}"
        f" R4={s.r4_examples} R5={s.r5_failures} R7={s.r7_density}"
        + ("" if evals_ok is None else f" R8={2 if evals_ok else 0}")
        + f"; pending {', '.join(pending)}"
        + (" (reviewer)" if evals_ok is not None else " (R8 from evals; the rest a reviewer)")
    )


__all__ = ["RUBRIC_BAR", "RUBRIC_TOTAL", "RubricScore", "auto_rubric", "score"]
