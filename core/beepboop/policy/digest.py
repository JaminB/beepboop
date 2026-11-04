"""Canonical JSON and the approval ``args_digest`` (IDENTITY_AND_TRUST §7.2, §8.2)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping


def canonical_json(args: Mapping[str, object]) -> str:
    """Sorted keys, no whitespace, no NaN: the same args always give the same bytes."""
    return json.dumps(
        args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def args_digest(tool: str, args: Mapping[str, object], target_uuids: Iterable[str]) -> str:
    """``sha256(tool || canonical_json(args) || sorted target uuids)`` as hex."""
    h = hashlib.sha256()
    h.update(tool.encode())
    h.update(b"\x00")
    h.update(canonical_json(args).encode())
    h.update(b"\x00")
    h.update("\x1f".join(sorted(target_uuids)).encode())
    return h.hexdigest()
