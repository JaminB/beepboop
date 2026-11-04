"""The entity resolver (docs/HOME_ASSISTANT.md §6.3).

``resolve(principal, query, index)`` is a server-side, pure and deterministic function
generic over an :class:`EntityIndex`; ``core/beepboop/ha/`` supplies the HA-backed index
(``beepboop.ha.index.HAEntityIndex``) and :class:`StaticEntityIndex` serves tests, evals
and the fast-path harness. The model never sees entity ids; it passes words.

Scoring is rapidfuzz ``token_set_ratio`` over the entity's name, registry aliases and
``entity_id`` object id; areas and floors match by name and alias at 85/100. The
thresholds (match 80, ok 90, ambiguity band 10) are the §6.3 targets; the Hypothesis
properties in ``core/tests/registry/test_resolver_properties.py`` pin the invariants.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from rapidfuzz import fuzz

from beepboop.module.types import EntityRef
from beepboop.policy.types import Principal

ResolutionStatus = Literal["ok", "ambiguous", "none", "denied"]

#: Fast-path threshold: ``status == ok`` and ``confidence >= 0.9`` (HOME_ASSISTANT §6.3).
FASTPATH_MIN_CONFIDENCE = 0.9
MATCH_THRESHOLD = 80
OK_THRESHOLD = 90
AMBIGUITY_BAND = 10
AREA_THRESHOLD = 85
#: At most this many candidates go back for the clarifying question.
MAX_CANDIDATES = 3

_ARTICLES = frozenset({"the", "a", "an", "my", "our", "please", "in", "on", "of"})
#: Words that make a query plural without naming anything ("all the lights", "everything").
_QUANTIFIERS = frozenset({"all", "every", "everything", "both"})
_PLURALS = {
    "lights": "light",
    "lamps": "lamp",
    "fans": "fan",
    "blinds": "blind",
    "curtains": "curtain",
    "shades": "shade",
    "speakers": "speaker",
    "locks": "lock",
    "switches": "switch",
    "doors": "door",
    "windows": "window",
    "plugs": "plug",
    "outlets": "outlet",
    "heaters": "heater",
    "tvs": "tv",
    "sensors": "sensor",
}
#: Domain nouns (HOME_ASSISTANT §6.3 step 3): a bare noun matches every entity of its domain.
_DOMAIN_NOUNS: dict[str, str] = {
    "light": "light",
    "lamp": "light",
    "fan": "fan",
    "tv": "media_player",
    "television": "media_player",
    "telly": "media_player",
    "speaker": "media_player",
    "lock": "lock",
    "blind": "cover",
    "curtain": "cover",
    "shade": "cover",
    "thermostat": "climate",
    "heating": "climate",
    "ac": "climate",
    "switch": "switch",
    "plug": "switch",
    "outlet": "switch",
    "vacuum": "vacuum",
}


@dataclass(frozen=True, slots=True)
class Area:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    floor_id: str | None = None


@dataclass(frozen=True, slots=True)
class Floor:
    id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetQuery:
    """What the person said, split into slots. ``all`` is MCP §4's ``target.all``."""

    name: str | None = None
    area: str | None = None
    domain: str | None = None
    device_class: str | None = None
    state_filter: str | None = None
    all: bool = False


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolutionStatus
    targets: tuple[EntityRef, ...] = ()
    candidates: tuple[tuple[EntityRef, float], ...] = ()
    denied: tuple[EntityRef, ...] = ()
    confidence: float = 0.0

    @property
    def fastpath_ok(self) -> bool:
        return self.status == "ok" and self.confidence >= FASTPATH_MIN_CONFIDENCE


class EntityIndex(Protocol):
    """What the resolver reads. ``visible`` is the principal's granted view (§6.2):
    active, not hidden, not disabled, covered by a grant. ``all_active`` is the same
    without the grant filter (step 5 uses it to tell "not granted" from "unknown")."""

    def visible(self, principal: Principal) -> Sequence[EntityRef]: ...
    def all_active(self) -> Sequence[EntityRef]: ...
    def areas(self) -> Sequence[Area]: ...
    def floors(self) -> Sequence[Floor]: ...
    def state_of(self, entity: EntityRef) -> str | None: ...


@dataclass
class StaticEntityIndex:
    """An in-memory :class:`EntityIndex` for tests, evals and the fast-path harness."""

    entities: list[EntityRef]
    area_list: list[Area] = field(default_factory=list[Area])
    visible_uuids: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    states: dict[str, str] = field(default_factory=dict[str, str])
    floor_list: list[Floor] = field(default_factory=list[Floor])

    def visible(self, principal: Principal) -> Sequence[EntityRef]:
        allowed = self.visible_uuids.get(principal.profile_id)
        if allowed is None:
            return self.all_active()
        return [e for e in self.all_active() if e.uuid in allowed]

    def all_active(self) -> Sequence[EntityRef]:
        return [e for e in self.entities if not (e.hidden or e.disabled)]

    def areas(self) -> Sequence[Area]:
        return self.area_list

    def floors(self) -> Sequence[Floor]:
        return self.floor_list

    def state_of(self, entity: EntityRef) -> str | None:
        return self.states.get(entity.uuid)


# ------------------------------------------------------------------ normalising --


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("'s", ""))


def normalise(text: str) -> list[str]:
    """Lower, strip articles and quantifiers, singularise common nouns (§6.3 step 3)."""
    words = [w for w in _words(text) if w not in _ARTICLES and w not in _QUANTIFIERS]
    return [_PLURALS.get(w) or w for w in words]


def is_plural(text: str) -> bool:
    """True for "lights", "all the lamps", "everything"."""
    return any(w in _PLURALS or w in _QUANTIFIERS for w in _words(text))


def token_set_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """0..100 rapidfuzz ``token_set_ratio`` on already-normalised token lists."""
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(" ".join(a), " ".join(b)))


def _names(entity: EntityRef) -> list[list[str]]:
    object_id = entity.entity_id.split(".", 1)[-1].replace("_", " ")
    return [normalise(n) for n in (entity.name, *entity.aliases, object_id) if n]


def _score(entity: EntityRef, tokens: list[str]) -> float:
    """``token_set_ratio`` (a name the person shortened, "ceiling" for "Kitchen ceiling",
    still scores 100), except when the person said *more* than the name: then the extra
    words count against it (``token_sort_ratio``), so "garage door" does not match an
    alias "door" as well as it matches "Garage door"."""
    if not tokens:
        return 100.0  # an area or domain-noun query ("the kitchen", "the lights")
    best = 0.0
    query = set(tokens)
    for name in _names(entity):
        if name == tokens or set(name) == query:
            return 100.0
        if set(name) < query:
            score = float(fuzz.token_sort_ratio(" ".join(tokens), " ".join(name)))
        else:
            score = token_set_ratio(tokens, name)
        best = max(best, score)
    return best


# ------------------------------------------------------------------------ areas --


@dataclass(frozen=True, slots=True)
class _Place:
    """A matched area or floor, expressed as the set of area ids it covers."""

    area_ids: frozenset[str]
    floor_id: str | None
    score: float


def _place_names(names: Sequence[str]) -> list[list[str]]:
    return [normalise(n) for n in names if normalise(n)]


def _match_place(index: EntityIndex, spoken: str) -> _Place | None:
    """Fuzzy-match ``spoken`` against area and floor names and aliases (threshold 85)."""
    tokens = normalise(spoken)
    if not tokens:
        return None
    best: _Place | None = None
    for area in sorted(index.areas(), key=lambda a: a.id):
        names = _place_names([area.name, *area.aliases, area.id.replace("_", " ")])
        score = max((float(fuzz.ratio(" ".join(tokens), " ".join(n))) for n in names), default=0)
        if score >= AREA_THRESHOLD and (best is None or score > best.score):
            best = _Place(frozenset({area.id}), None, score)
    for floor in sorted(index.floors(), key=lambda f: f.id):
        names = _place_names([floor.name, *floor.aliases, floor.id.replace("_", " ")])
        score = max((float(fuzz.ratio(" ".join(tokens), " ".join(n))) for n in names), default=0)
        if score >= AREA_THRESHOLD and (best is None or score > best.score):
            ids = frozenset(a.id for a in index.areas() if a.floor_id == floor.id)
            best = _Place(ids, floor.id, score)
    return best


def _split_place_from_name(index: EntityIndex, name: str) -> tuple[_Place | None, str]:
    """ "kitchen lights" → (kitchen, "lights"): the longest area/floor name whose tokens all
    appear in ``name`` is taken out of it (§6.3 step 2)."""
    tokens = normalise(name)
    best: tuple[int, _Place, set[str]] | None = None
    places: list[tuple[list[str], _Place]] = []
    for area in sorted(index.areas(), key=lambda a: a.id):
        place = _Place(frozenset({area.id}), None, 100.0)
        for n in _place_names([area.name, *area.aliases, area.id.replace("_", " ")]):
            places.append((n, place))
    for floor in sorted(index.floors(), key=lambda f: f.id):
        ids = frozenset(a.id for a in index.areas() if a.floor_id == floor.id)
        place = _Place(ids, floor.id, 100.0)
        for n in _place_names([floor.name, *floor.aliases, floor.id.replace("_", " ")]):
            places.append((n, place))
    for place_tokens, place in places:
        wanted = set(place_tokens)
        if wanted <= set(tokens) and (best is None or len(wanted) > best[0]):
            best = (len(wanted), place, wanted)
    if best is None:
        return None, name
    rest = [t for t in tokens if t not in best[2]]
    return best[1], " ".join(rest)


def _in_place(entity: EntityRef, place: _Place | None) -> bool:
    if place is None:
        return True
    if place.floor_id is not None and entity.floor_id == place.floor_id:
        return True
    return entity.area_id in place.area_ids


# ----------------------------------------------------------------------- filter --


def _apply_filters(
    entities: Sequence[EntityRef],
    query: TargetQuery,
    place: _Place | None,
    index: EntityIndex,
) -> list[EntityRef]:
    out: list[EntityRef] = []
    for e in entities:
        if query.domain and e.domain != query.domain:
            continue
        if query.device_class and e.device_class != query.device_class:
            continue
        if not _in_place(e, place):
            continue
        if query.state_filter and index.state_of(e) != query.state_filter:
            continue
        out.append(e)
    return out


def _rank(entities: Sequence[EntityRef], tokens: list[str]) -> list[tuple[EntityRef, float]]:
    scored = [(e, _score(e, tokens)) for e in entities]
    scored = [(e, s) for e, s in scored if s >= MATCH_THRESHOLD]
    scored.sort(key=lambda es: (-es[1], es[0].entity_id, es[0].uuid))
    return scored


def _exact_entity_name(entities: Sequence[EntityRef], tokens: list[str]) -> bool:
    return bool(tokens) and any(n == tokens for e in entities for n in _names(e))


def resolve(principal: Principal, query: TargetQuery, index: EntityIndex) -> Resolution:
    """HOME_ASSISTANT §6.3 steps 1-5 over ``index``. Pure and deterministic."""
    plural = query.all or is_plural(query.name or "")

    # 2. Area: explicit, else split out of the name, unless the whole name is a device.
    place: _Place | None = None
    name = query.name or ""
    if query.area:
        place = _match_place(index, query.area)
        if place is None:
            return Resolution("none")
    elif name:
        split_place, rest = _split_place_from_name(index, name)
        if split_place is not None and not _exact_entity_name(
            index.visible(principal), normalise(name)
        ):
            place, name = split_place, rest
    tokens = normalise(name)
    if not tokens and place is None and not query.domain:
        return Resolution("none")

    # 3. A bare domain noun ("lights", "the tv") names a domain, not a device.
    domain_noun = _DOMAIN_NOUNS.get(tokens[0]) if len(tokens) == 1 else None
    if domain_noun is not None and query.domain is None:
        query = TargetQuery(
            query.name, query.area, domain_noun, query.device_class, query.state_filter, query.all
        )

    # 1. Scope: the principal's view, then domain / device_class / state filters.
    scoped = _apply_filters(index.visible(principal), query, place, index)
    ranked = _rank(scoped, tokens)
    if domain_noun is not None and (plural or not ranked):
        ranked = _rank(scoped, [])  # every entity of the domain in scope
    elif query.all and not tokens:
        ranked = _rank(scoped, [])

    # 4. Decide.
    if ranked:
        best_score = ranked[0][1]
        top = [es for es in ranked if es[1] >= best_score - AMBIGUITY_BAND]
        same_area_domain = len({(e.area_id, e.domain) for e, _ in top}) == 1
        if best_score >= OK_THRESHOLD and (len(top) == 1 or same_area_domain or plural):
            return Resolution(
                "ok",
                tuple(e for e, _ in top),
                tuple(top[:MAX_CANDIDATES]),
                (),
                best_score / 100,
            )
        return Resolution("ambiguous", (), tuple(top[:MAX_CANDIDATES]), (), best_score / 100)

    # 5. Would the full index have matched? Then it is a grant problem, not vocabulary.
    visible_ids = {e.uuid for e in index.visible(principal)}
    hidden = [
        e
        for e in _apply_filters(index.all_active(), query, place, index)
        if e.uuid not in visible_ids
    ]
    denied = [e for e, _ in _rank(hidden, tokens)]
    if domain_noun is not None and not denied:
        denied = list(hidden)
    if denied:
        if principal.is_guest:
            return Resolution("none")  # never named, never counted, to Guests
        return Resolution("denied", (), (), tuple(denied), 0.0)
    return Resolution("none")


__all__ = [
    "FASTPATH_MIN_CONFIDENCE",
    "Area",
    "EntityIndex",
    "Floor",
    "Resolution",
    "ResolutionStatus",
    "StaticEntityIndex",
    "TargetQuery",
    "is_plural",
    "normalise",
    "resolve",
    "token_set_ratio",
]
