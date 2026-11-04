"""The deterministic fast path (docs/LLM_PROVIDERS.md §12).

Grammar files, not code: ``grammar/<locale>.yaml`` lists templates per intent with slots
and the tool call each one emits. ``FastPath.match`` is steps 1-2 of §12.2 (normalise,
grammar match); :meth:`FastPathMatch.build_args` turns the slots into the tool's
arguments (the same call the LLM would make). Steps 3-6 (resolve, authorize through the
same ``registry.dispatch``, execute, speak) are the turn loop's. A match never carries
privileges: it is a different *planner*, not a different path to effect. Conservative
by construction: any doubt returns ``None`` and the LLM takes over.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, cast

import yaml

from beepboop.registry.resolver import TargetQuery

GRAMMAR_DIR = Path(__file__).with_name("grammar")

_SLOT_PATTERNS: dict[str, str] = {
    # a device name never starts with a question word, so "is the fan on" is a state query,
    # not "turn on <is the fan>" (keeps every template to exactly one intent)
    "target": r"(?P<target>(?!(?:is|are|what|how|turn|switch) )[a-z0-9 ]+?)",
    "area": r"(?P<area>[a-z0-9 ]+?)",
    "label": r"(?P<label>[a-z0-9 ]+?)",
    "number": r"(?P<number>\d+)",
    "percent": r"(?P<percent>\d{1,3})",
    "duration": r"(?P<duration>\d+ (?:second|minute|hour)s?(?: and \d+ (?:second|minute)s?)?)",
    "clock_time": r"(?P<clock_time>\d{1,2}(?::\d{2})?(?: ?[ap]m)?)",
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
_CONTRACTIONS = {
    "what's": "what is",
    "it's": "it is",
    "that's": "that is",
    "wasn't": "was not",
    "isn't": "is not",
    "i'd": "i would",
}
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600}


class GrammarError(ValueError):
    """``grammar/<locale>.yaml`` is malformed."""


@dataclass(frozen=True, slots=True)
class Template:
    say: str
    pattern: re.Pattern[str]
    args: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Intent:
    name: str
    tool: str | None
    domain: str | None
    templates: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...] = field(repr=False, default=())
    entries: tuple[Template, ...] = field(repr=False, default=())


@dataclass(frozen=True, slots=True)
class FastPathMatch:
    intent: str
    tool: str | None
    slots: dict[str, str]
    query: TargetQuery | None
    args_spec: Mapping[str, object] = field(default_factory=dict[str, object])
    domain: str | None = None
    template: str = ""

    @property
    def is_core_action(self) -> bool:
        """``not_me``, ``forget_last``, ``stop``, ``time_date``: no tool is dispatched."""
        return self.tool is None

    def build_args(self, now: datetime) -> dict[str, Any] | None:
        """The tool arguments, or ``None`` when a slot cannot be converted (→ LLM).

        ``now`` must be aware and in the household timezone (``$at`` keeps its offset).
        """
        try:
            return {k: self._value(v, now) for k, v in self.args_spec.items()}
        except (ValueError, KeyError):
            return None

    def _value(self, spec: object, now: datetime) -> object:
        if isinstance(spec, dict):
            return {str(k): self._value(v, now) for k, v in cast(dict[str, object], spec).items()}
        if not (isinstance(spec, str) and spec.startswith("$")):
            return spec
        name = spec[1:]
        if name == "target":
            target: dict[str, object] = {}
            if "target" in self.slots:
                target["name"] = self.slots["target"]
            if "area" in self.slots:
                target["area"] = self.slots["area"]
            if not target:
                raise KeyError("target")
            if self.domain:
                target["domain"] = self.domain
            return target
        if name in ("percent", "number"):
            value = int(self.slots[name])
            if name == "percent" and not 0 <= value <= 100:
                raise ValueError(name)
            return value
        if name == "duration_s":
            return parse_duration_s(self.slots["duration"])
        if name == "at":
            return next_clock_time(self.slots["clock_time"], now).isoformat()
        if name == "label":
            return self.slots["label"]
        if name == "items":
            return [self.slots["label"]]
        raise KeyError(name)


def parse_duration_s(text: str) -> int:
    """``"10 minutes"``, ``"1 hour and 5 minutes"`` → seconds (1 … 86 400)."""
    total = 0
    for amount, unit in re.findall(r"(\d+) (second|minute|hour)s?", text):
        total += int(amount) * _UNIT_S[unit]
    if not 1 <= total <= 86_400:
        raise ValueError("duration out of range")
    return total


def next_clock_time(text: str, now: datetime) -> datetime:
    """The next occurrence of a spoken clock time after ``now`` (same tz as ``now``).

    Without am/pm, an hour ≤ 12 means whichever of ``h`` and ``h + 12`` comes first.
    """
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))? ?([ap]m)?", text.strip())
    if m is None:
        raise ValueError(text)
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59 or hour > 23 or (ampm and not 1 <= hour <= 12):
        raise ValueError(text)
    if ampm:
        hour = hour % 12 + (12 if ampm == "pm" else 0)
        hours = [hour]
    else:
        hours = [hour, hour + 12] if hour < 12 else [hour]
        if hour == 12:
            hours = [12, 0]
    candidates: list[datetime] = []
    for h in hours:
        at = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        candidates.append(at)
    return min(candidates)


def _compile(template: str) -> re.Pattern[str]:
    out = ""
    pos = 0
    for m in re.finditer(r"\{(\w+)\}", template):
        out += re.escape(template[pos : m.start()])
        slot = m.group(1)
        if slot not in _SLOT_PATTERNS:
            raise GrammarError(f"unknown slot {{{slot}}} in {template!r}")
        out += _SLOT_PATTERNS[slot]
        pos = m.end()
    out += re.escape(template[pos:])
    return re.compile(rf"^{out}$")


@dataclass(frozen=True)
class Grammar:
    locale: str
    fillers: tuple[str, ...]
    intents: tuple[Intent, ...]

    @classmethod
    def parse(cls, locale: str, text: str) -> Grammar:
        raw: object = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise GrammarError("grammar must be a mapping")
        doc = cast(dict[str, Any], raw)
        fillers = tuple(str(f) for f in doc.get("fillers", []))
        intents: list[Intent] = []
        for name, body in cast(dict[str, Any], doc.get("intents", {})).items():
            spec = cast(dict[str, Any], body)
            base_args = cast(dict[str, object], spec.get("args") or {})
            entries: list[Template] = []
            for item in cast(list[Any], spec.get("templates", [])):
                if isinstance(item, str):
                    say, extra = item, {}
                elif isinstance(item, dict) and "say" in item:
                    entry = cast(dict[str, Any], item)
                    say, extra = str(entry["say"]), cast(dict[str, object], entry.get("args") or {})
                else:
                    raise GrammarError(f"intent {name}: bad template {item!r}")
                entries.append(Template(say, _compile(say), {**base_args, **extra}))
            if not entries:
                raise GrammarError(f"intent {name} has no templates")
            tool = spec.get("tool")
            intents.append(
                Intent(
                    str(name),
                    None if tool is None else str(tool),
                    None if spec.get("domain") is None else str(spec["domain"]),
                    tuple(e.say for e in entries),
                    tuple(e.pattern for e in entries),
                    tuple(entries),
                )
            )
        return cls(locale, fillers, tuple(intents))


@cache
def load_grammar(locale: str = "en", directory: Path = GRAMMAR_DIR) -> Grammar:
    lang = locale.split("-", 1)[0].lower()
    path = directory / f"{lang}.yaml"
    if not path.exists():
        raise GrammarError(f"no fast-path grammar for locale {locale!r}")
    return Grammar.parse(lang, path.read_text(encoding="utf-8"))


def normalise(text: str, fillers: tuple[str, ...]) -> str:
    """Lowercase, strip wake-word residue and fillers, words → numbers (§12.2 step 1)."""
    t = text.lower().strip().replace(chr(0x2019), "'")
    t = re.sub(r"[^\w\s:']", " ", t)
    for short, full in _CONTRACTIONS.items():
        t = re.sub(rf"\b{re.escape(short)}(?=\s|$)", full, t)
    for f in sorted(fillers, key=len, reverse=True):
        t = re.sub(rf"\b{re.escape(f)}\b", " ", t)
    words = [str(_NUMBER_WORDS[w]) if w in _NUMBER_WORDS else w for w in t.split()]
    return " ".join(words).strip()


class FastPath:
    """Grammar matcher for one locale. ``match`` is pure and deterministic."""

    def __init__(self, grammar: Grammar) -> None:
        self.grammar = grammar

    @classmethod
    def for_locale(cls, locale: str = "en") -> FastPath:
        return cls(load_grammar(locale))

    def hits(self, text: str) -> list[tuple[Intent, Template, dict[str, str]]]:
        """Every intent with a matching template (first template per intent)."""
        normalised = normalise(text, self.grammar.fillers)
        out: list[tuple[Intent, Template, dict[str, str]]] = []
        if not normalised:
            return out
        for intent in self.grammar.intents:
            for entry in intent.entries:
                m = entry.pattern.match(normalised)
                if m:
                    slots = {k: v.strip() for k, v in m.groupdict().items() if v and v.strip()}
                    out.append((intent, entry, slots))
                    break
        return out

    def match(self, text: str) -> FastPathMatch | None:
        """One intent or nothing: more than one matching intent → ``None`` (let the LLM decide)."""
        hits = self.hits(text)
        if len(hits) != 1:
            return None
        intent, entry, slots = hits[0]
        query = None
        if "target" in slots or "area" in slots:
            query = TargetQuery(
                name=slots.get("target"), area=slots.get("area"), domain=intent.domain
            )
        return FastPathMatch(
            intent.name, intent.tool, slots, query, entry.args, intent.domain, entry.say
        )


__all__ = [
    "GRAMMAR_DIR",
    "FastPath",
    "FastPathMatch",
    "Grammar",
    "GrammarError",
    "Intent",
    "Template",
    "load_grammar",
    "next_clock_time",
    "normalise",
    "parse_duration_s",
]
