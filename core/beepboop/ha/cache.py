"""The in-memory state cache (HOME_ASSISTANT §5).

``entity_id → State`` seeded by ``get_states`` and kept current by ``state_changed``.
Not persisted: HA is the source of truth, and after a reconnect the cache is replaced
wholesale. While the connection is down the cache is marked ``stale`` and ``home.state``
answers from it with ``stale: true``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

type JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class State:
    entity_id: str
    state: str
    attributes: Mapping[str, Any] = field(default_factory=dict[str, Any])
    last_changed: str | None = None
    last_updated: str | None = None
    context_id: str | None = None

    @classmethod
    def from_ha(cls, raw: Mapping[str, object]) -> State | None:
        """Parse one ``get_states`` row or ``state_changed.new_state``; ``None`` if malformed."""
        entity_id = raw.get("entity_id")
        state = raw.get("state")
        if not isinstance(entity_id, str) or not isinstance(state, str):
            return None
        attrs_raw = raw.get("attributes")
        attrs = dict(cast("dict[str, Any]", attrs_raw)) if isinstance(attrs_raw, dict) else {}
        context = raw.get("context")
        context_id: str | None = None
        if isinstance(context, dict):
            cid = cast("JsonObject", context).get("id")
            context_id = cid if isinstance(cid, str) else None
        changed = raw.get("last_changed")
        updated = raw.get("last_updated")
        return cls(
            entity_id=entity_id,
            state=state,
            attributes=attrs,
            last_changed=changed if isinstance(changed, str) else None,
            last_updated=updated if isinstance(updated, str) else None,
            context_id=context_id,
        )


class StateCache:
    """Dictionary lookups for ``home.state`` and the resolver's ``state_filter``."""

    def __init__(self) -> None:
        self._states: dict[str, State] = {}
        self.stale = True

    def replace_all(self, rows: Iterable[Mapping[str, object]]) -> None:
        fresh: dict[str, State] = {}
        for row in rows:
            parsed = State.from_ha(row)
            if parsed is not None:
                fresh[parsed.entity_id] = parsed
        self._states = fresh
        self.stale = False

    def apply_state_changed(self, data: Mapping[str, object]) -> None:
        """``state_changed`` event ``data``: ``new_state: null`` removes the entity."""
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        new_raw = data.get("new_state")
        if new_raw is None:
            self._states.pop(entity_id, None)
            return
        if isinstance(new_raw, dict):
            parsed = State.from_ha(cast("JsonObject", new_raw))
            if parsed is not None:
                self._states[entity_id] = parsed

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    def all(self) -> Mapping[str, State]:
        return self._states

    def __len__(self) -> int:
        return len(self._states)


__all__ = ["State", "StateCache"]
