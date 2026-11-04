"""Deterministic JSON Schema for the event protocol (CLAUDE.md §5).

``protocol_schema()`` returns one document with an ``audio_to_core`` and a
``core_to_audio`` schema, generated from the pydantic models; ``schema_json()``
serializes it with sorted keys so the output is byte-stable across runs.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter

from beepboop_protocol.audio_events import AudioEvent
from beepboop_protocol.core_events import CoreEvent
from beepboop_protocol.envelope import PROTOCOL_MAJOR


def protocol_schema() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_MAJOR,
        "audio_to_core": TypeAdapter(AudioEvent).json_schema(mode="serialization"),
        "core_to_audio": TypeAdapter(CoreEvent).json_schema(mode="serialization"),
    }


def schema_json() -> str:
    return json.dumps(protocol_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = ["protocol_schema", "schema_json"]
