"""Word error rate for ``bench stt`` and ``bench aec``.

Text is normalised the same way on both sides: lower-case, punctuation
stripped, whitespace collapsed, digits kept. WER is substitutions +
deletions + insertions over reference words (Levenshtein on words).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_PUNCT = re.compile(r"[^\w\s']+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> list[str]:
    """Tokenise for scoring."""
    cleaned = _PUNCT.sub(" ", text.lower())
    cleaned = _SPACES.sub(" ", cleaned).strip()
    return cleaned.split(" ") if cleaned else []


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """``(errors, reference_words)`` for one pair."""
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return len(hyp), 0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1], len(ref)


def wer_pct(pairs: Iterable[tuple[str, str]]) -> float | None:
    """Corpus-level WER in percent; ``None`` when there are no reference words."""
    errors = 0
    words = 0
    for reference, hypothesis in pairs:
        e, n = word_errors(reference, hypothesis)
        errors += e
        words += n
    return 100.0 * errors / words if words else None
