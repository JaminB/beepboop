"""Sentence chunking (VOICE_PIPELINE §11.2), system phrases (§11.6) and cost (LLM_PROVIDERS §10)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from hypothesis import given
from hypothesis import strategies as st

from beepboop.agent.chunking import SOFT_LIMIT, SentenceChunker, clean_for_speech
from beepboop.agent.phrases import (
    PHRASES,
    TIER_WORDS,
    deny_phrase,
    deny_speech,
    phrase,
    placeholders,
)
from beepboop.agent.usage import TurnUsage, load_pricing
from beepboop.policy.types import DenyReason, ProfileKind
from beepboop.providers.llm.protocol import Usage

DOCS = Path(__file__).resolve().parents[3] / "docs"


# ------------------------------------------------------------------- chunking --


def _chunk(deltas: list[str]) -> tuple[list[str], str | None]:
    c = SentenceChunker()
    out: list[str] = []
    for d in deltas:
        out.extend(c.feed(d))
    return out, c.finish()


def test_sentences_leave_when_the_next_one_starts() -> None:
    c = SentenceChunker()
    assert c.feed("Kitchen lights are off.") == []  # held: it might be the last
    assert c.feed(" The") == ["Kitchen lights are off."]
    assert c.feed(" fan too.") == []
    assert c.finish() == "The fan too."


def test_boundaries_are_dot_question_bang_semicolon_colon() -> None:
    sent, last = _chunk(["One. Two? Three! Four; Five: six"])
    assert sent == ["One.", "Two?", "Three!", "Four;", "Five:"]
    assert last == "six"


def test_decimal_points_do_not_split() -> None:
    sent, last = _chunk(["It is 21.5 degrees. Nice."])
    assert sent == ["It is 21.5 degrees."]
    assert last == "Nice."


def test_soft_limit_cuts_at_a_space() -> None:
    long = "word " * 40
    sent, last = _chunk([long, "end."])
    assert all(len(s) <= SOFT_LIMIT for s in sent)
    assert last is not None


@given(st.lists(st.text(alphabet="abc .?!", max_size=15), max_size=12))
def test_chunking_loses_no_words(deltas: list[str]) -> None:
    sent, last = _chunk(deltas)
    spoken = " ".join([*sent, *([last] if last else [])])
    assert spoken.split() == clean_for_speech("".join(deltas)).split()


def test_clean_for_speech() -> None:
    assert clean_for_speech("# Title\n- **bold** item\n`code` [here](http://x)") == (
        "Title bold item code here"
    )
    assert clean_for_speech("see https://example.com now") == "see now"


# -------------------------------------------------------------------- phrases --


def test_phrase_table_matches_voice_pipeline_11_6() -> None:
    """The texts are the §11.6 defaults, verbatim (one owner per fact)."""
    doc = (DOCS / "VOICE_PIPELINE.md").read_text(encoding="utf-8")
    table = doc[doc.index("### 11.6") : doc.index("## 12.")]
    rows = dict(re.findall(r"^\| `([a-z_.]+)` \| \"(.+?)\" \|", table, flags=re.M))
    assert rows == PHRASES


KINDS: tuple[ProfileKind, ...] = get_args(ProfileKind)
REASONS: tuple[DenyReason, ...] = get_args(DenyReason)


@given(
    st.sampled_from(REASONS),
    st.sampled_from(KINDS),
    st.sampled_from(["home.set", "caldav.read", "x.y"]),
)
def test_phrase_rules(reason: DenyReason, kind: ProfileKind, tool: str) -> None:
    """§11.6 rule 1: a guest is never told to approve on a phone. Rule 2: no tier names."""
    speech = deny_speech(reason, kind, tool=tool, name="Mia", admin="Alex")
    text = speech.text or PHRASES[speech.clip or ""]
    if kind == "guest":
        assert speech.clip == "unknown_speaker"
        assert "phone" not in text
    assert not TIER_WORDS.search(text)
    assert deny_phrase(reason, kind) != "ceiling.child" or kind == "child"


def test_placeholder_keys_send_text_and_clip() -> None:
    plain = phrase("offline")
    assert plain.clip == "offline"
    assert plain.text is None
    filled = phrase("approval.prompt", summary="unlock the front door")
    assert filled.clip == "approval.prompt"
    assert filled.text == "To unlock the front door, approve on your phone."
    assert placeholders("approval.no_login") == {"admin"}


def test_no_phrase_says_a_tier_name() -> None:
    for text in PHRASES.values():
        assert not TIER_WORDS.search(text)


# ---------------------------------------------------------------------- usage --


def test_pricing_loads_every_row() -> None:
    prices = load_pricing()
    haiku = prices["global.anthropic.claude-haiku-5-5"]
    assert haiku.input == 0.10
    assert haiku.estimate  # verify: true rows are estimates (§10)


def test_turn_usage_cost_in_micro_usd() -> None:
    u = TurnUsage()
    u.add(
        Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=2000, cache_write_tokens=0),
        "global.anthropic.claude-haiku-5-5",
    )
    # 1000*0.10 + 100*0.50 + 2000*0.01 = 170 micro-USD
    assert u.cost_micro_usd == 170
    u.add(
        Usage(input_tokens=10, output_tokens=10, cache_write_tokens=1000),
        "global.anthropic.claude-haiku-5-5",
        ttl="1h",
    )
    assert u.cost_micro_usd == 170 + round(10 * 0.10 + 10 * 0.50 + 1000 * 0.20)
    assert u.calls == 2
    unknown = TurnUsage()
    unknown.add(Usage(input_tokens=1, output_tokens=1), "nope")
    assert not unknown.priced
    assert unknown.cost_micro_usd == 0
