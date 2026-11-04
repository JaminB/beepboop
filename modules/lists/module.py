"""lists: shared household lists and personal lists (docs/MODULES.md §16).

Storage (no core tables): one kv value per list, key ``list:<name>``, value
``{"name": str, "items": [{"text": str, "added_at": iso}]}``. Shared lists live in
``ctx.kv.household()``; "my" lists live in the speaker's profile scope and are deleted with
the profile. No network, no HA: lists work offline through the fast path.

Who added an item is deliberately not stored: a shared list is the household's, and
"delete me" leaves nothing attributable in it (docs/DATA_AND_PRIVACY.md §9.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from beepboop.module import KVStore, ToolContext, ToolError, tool
from beepboop.module import Module as BaseModule

KEY_PREFIX = "list:"
MAX_ITEMS = 200  # per list; a 200-item list stays far under the 64 KiB kv value limit
MAX_LISTS = 30  # per scope
READ_ALOUD = 10

# Spoken list names → one canonical name. Everything else is used as said.
_ALIASES = {
    "shopping": "shopping",
    "grocery": "shopping",
    "groceries": "shopping",
    "grocery shopping": "shopping",
    "todo": "to-do",
    "to do": "to-do",
    "to-do": "to-do",
    "to dos": "to-do",
    "to-dos": "to-do",
    "todos": "to-do",
}
_DROP_WORDS = ("list", "the", "my", "our")


@dataclass(slots=True)
class _List:
    name: str
    items: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    @property
    def texts(self) -> list[str]:
        return [i["text"] for i in self.items]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "items": self.items}


class Module(BaseModule):
    """Shared and personal lists. Stateless apart from ctx.kv."""

    @tool("list.add")
    async def add(
        self,
        ctx: ToolContext,
        items: list[str],
        list: str = "shopping",  # noqa: A002 - the manifest arg is "list" (docs/MCP.md §4.9)
        personal: bool = False,
    ) -> dict[str, Any]:
        name = canonical_name(list)
        kv = _scope(ctx, personal)
        current = await _load(kv, name)
        if current is None:
            if len(await kv.keys(KEY_PREFIX)) >= MAX_LISTS:
                raise ToolError(
                    f"There are already {MAX_LISTS} lists. Ask the user to clear one in the app.",
                    code="invalid_args",
                )
            current = _List(name)
        added, already = merge(current, items, ctx.now())
        if len(current.items) > MAX_ITEMS:
            raise ToolError(
                f"The {name} list is full ({MAX_ITEMS} items). Ask the user to clear some first.",
                code="invalid_args",
            )
        if added:
            await kv.set(KEY_PREFIX + name, current.to_dict())
        ctx.log.info("lists.add", personal=personal, n=len(added))  # never log item text
        return {
            "speak": add_text(added, already, _spoken_list(name, personal)),
            "list": name,
            "added": added,
            "already": already,
            "count": len(current.items),
        }

    @tool("list.read")
    async def read(
        self,
        ctx: ToolContext,
        list: str = "shopping",  # noqa: A002 - the manifest arg is "list"
        personal: bool = False,
    ) -> dict[str, Any]:
        name = canonical_name(list)
        current = await _load(_scope(ctx, personal), name)
        spoken = _spoken_list(name, personal)
        if current is None:
            return {"speak": no_list_text(name, personal), "list": name, "items": []}
        return {"speak": read_text(current.texts, spoken), "list": name, "items": current.texts}

    @tool("list.remove")
    async def remove(
        self,
        ctx: ToolContext,
        items: list[str] | None = None,
        list: str = "shopping",  # noqa: A002 - the manifest arg is "list"
        personal: bool = False,
        clear: bool = False,
    ) -> dict[str, Any]:
        name = canonical_name(list)
        kv = _scope(ctx, personal)
        spoken = _spoken_list(name, personal)
        current = await _load(kv, name)
        if current is None or not current.items:
            return {
                "speak": f"The {spoken} is already empty."
                if current is not None
                else no_list_text(name, personal),
                "list": name,
                "removed": [],
                "status": "none_matched",
            }
        if clear:
            removed = current.texts
            await kv.delete(KEY_PREFIX + name)
            return {
                "speak": f"Cleared the {spoken}.",
                "list": name,
                "removed": removed,
                "status": "ok",
            }
        if not items:
            raise ToolError(
                "Say which items to remove, or set clear for the whole list.", code="invalid_args"
            )
        removed, missing = take(current, items)
        if removed:
            await kv.set(KEY_PREFIX + name, current.to_dict())
        ctx.log.info("lists.remove", personal=personal, n=len(removed))
        return {
            "speak": remove_text(removed, missing, spoken),
            "list": name,
            "removed": removed,
            "status": "ok" if removed else "none_matched",
        }


# ------------------------------------------------- pure helpers (unit-tested directly)


def _scope(ctx: ToolContext, personal: bool) -> KVStore:
    if personal:
        if not ctx.profile_id:
            raise ToolError("Personal lists need to know who is asking.", code="other")
        return ctx.kv
    return ctx.kv.household()


async def _load(kv: KVStore, name: str) -> _List | None:
    raw: object = await kv.get(KEY_PREFIX + name)
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    rows: object = data.get("items", [])
    items: list[dict[str, str]] = []
    if isinstance(rows, list):
        for r in cast(list[object], rows):
            if isinstance(r, dict) and "text" in r:
                row = cast(dict[str, Any], r)
                items.append({"text": str(row["text"]), "added_at": str(row.get("added_at", ""))})
            elif isinstance(r, str):
                items.append({"text": r, "added_at": ""})
    return _List(str(data.get("name") or name), items)


def canonical_name(spoken: str) -> str:
    """Spoken name → key: "The Shopping List" → "shopping", "my to do" → "to-do"."""
    words = [w for w in spoken.lower().replace("_", " ").split() if w not in _DROP_WORDS]
    name = " ".join(words).strip(" .,'\"")
    if not name:
        return "shopping"
    return _ALIASES.get(name, name)[:40]


def clean_item(text: str) -> str:
    return " ".join(text.strip().strip(".,;").split()).lower()


def item_key(text: str) -> str:
    """Comparison key: case-, article- and simple-plural-insensitive ("Eggs" ≈ "egg")."""
    words = [w for w in clean_item(text).split() if w not in ("a", "an", "the", "some")]
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def merge(current: _List, items: list[str], now: datetime) -> tuple[list[str], list[str]]:
    """Append new items in order; return (added, already on the list)."""
    have = {item_key(t) for t in current.texts}
    added: list[str] = []
    already: list[str] = []
    for raw in items:
        text = clean_item(raw)
        if not text:
            continue
        key = item_key(text)
        if key in have:
            if text not in already:
                already.append(text)
            continue
        have.add(key)
        added.append(text)
        current.items.append({"text": text, "added_at": now.isoformat()})
    return added, already


def take(current: _List, items: list[str]) -> tuple[list[str], list[str]]:
    """Remove matching items; return (removed texts as stored, not found)."""
    removed: list[str] = []
    missing: list[str] = []
    for raw in items:
        key = item_key(raw)
        hit = next((i for i in current.items if item_key(i["text"]) == key), None)
        if hit is None:
            missing.append(clean_item(raw))
            continue
        current.items.remove(hit)
        removed.append(hit["text"])
    return removed, missing


def _spoken_list(name: str, personal: bool) -> str:
    return f"your {name} list" if personal else f"{name} list"


def no_list_text(name: str, personal: bool) -> str:
    return f"You don't have a {name} list." if personal else f"There's no {name} list."


def _the(spoken: str) -> str:
    return spoken if spoken.startswith("your ") else f"the {spoken}"


def join_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def add_text(added: list[str], already: list[str], spoken: str) -> str:
    parts: list[str] = []
    if added:
        parts.append(f"Added {join_words(added)} to {_the(spoken)}.")
    if already:
        verb = "is" if len(already) == 1 else "are"
        parts.append(f"{join_words(already).capitalize()} {verb} already on it.")
    return " ".join(parts) or "Nothing to add."


def read_text(texts: list[str], spoken: str) -> str:
    the = _the(spoken)
    if not texts:
        return f"{the.capitalize()} is empty."
    n = len(texts)
    noun = "item" if n == 1 else "items"
    shown = texts[:READ_ALOUD]
    more = f", and {n - READ_ALOUD} more" if n > READ_ALOUD else ""
    listed = join_words(shown) if n <= READ_ALOUD else ", ".join(shown)
    return f"{the.capitalize()} has {n} {noun}: {listed}{more}."


def remove_text(removed: list[str], missing: list[str], spoken: str) -> str:
    parts: list[str] = []
    the = _the(spoken)
    if removed:
        parts.append(f"Removed {join_words(removed)} from {the}.")
    if missing:
        verb = "isn't" if len(missing) == 1 else "aren't"
        parts.append(f"{join_words(missing).capitalize()} {verb} on {the}.")
    return " ".join(parts)
