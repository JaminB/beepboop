"""``beepboop.tokens.estimate()``: a cl100k-style token estimate (MODULES §4.6, L4).

No tokenizer dependency on the Pi: an approximation within about ±10 % on English prose
and JSON, which is what the lint's 10 % slack is for. Rule: whitespace-separated words
cost ~1.3 tokens, punctuation and structural characters cost one each.
"""

from __future__ import annotations

import math
import re

_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_PUNCT_RUN = re.compile(r"[^\w\s]+")


def estimate(text: str) -> int:
    """Approximate token count for ``text`` (never below 1 for non-empty input).

    Long words split roughly every five characters; a run of punctuation (``", ``, ``}``,
    ``->``) is one token.
    """
    if not text:
        return 0
    words = _WORD.findall(text)
    word_tokens = sum(max(1, math.ceil(len(w) / 5)) for w in words)
    punct = len(_PUNCT_RUN.findall(text))
    return max(1, word_tokens + punct)


__all__ = ["estimate"]
