# lists

Household lists everyone shares (shopping by default) and each person's own lists ("my to-do"). Tools: `list.add`, `list.read`, `list.remove`. Works without the internet. Items are written by household members, so reading them aloud is safe.

## When to use

- `list.add` — "add milk to the shopping list", "put eggs and butter on the list", "we're out of coffee", "we need paper towels", "remind me to buy batteries" (no time given), "add call the plumber to my to-do list", "put screws on the hardware store list".
- `list.read` — "what's on the shopping list", "read me the list", "what do we need from the store", "read my to-do list", "how many things are on the list", "is milk on the list".
- `list.remove` — "take milk off the list", "I got the eggs", "cross off bread", "remove call the plumber from my to-do", "clear the shopping list", "we bought everything".
- Overlaps: "remind me to buy milk at 5" → `timer.set` (reminder with a time); "add dentist to my calendar" → `caldav.create`.

## When NOT to use

- Ordering or buying online → not supported; offer to add it to the shopping list instead.
- Someone else's personal list ("what's on Bob's to-do") → decline: personal lists are private. Shared lists are fine for anyone with access.
- Recipes or meal plans → answer from general knowledge; add ingredients only if asked.

## Arguments

`list.add`
- `items`: one entry per thing, lowercase, singular where natural ("eggs" stays "eggs" if said so; duplicates are detected either way). Split "eggs and butter" into two items. Drop quantities unless the user stressed them ("2 lemons" → "2 lemons" is fine).
- `list`: omit for shopping. Use the name as said for others ("hardware store", "to-do", "packing"). "Groceries" means shopping.
- `personal: true` only when the user said "my" ("my to-do", "my list"). "The list" / "our list" are shared.
- Nothing to add ("add to the list") → ask "What should I add?".

`list.read`: same `list` / `personal` rules. For "is milk on the list", read the list and answer yes or no.

`list.remove`
- `items` as the user said them; matching ignores case, articles and a plural "s".
- `clear: true` for "clear the list", "we bought everything", "start a new list"; never for a single item.

## Examples

1. "Add milk to the shopping list." → `list.add {"items": ["milk"]}` → milk already there → "Milk is already on it."
2. "Put bread and butter on the list." → `list.add {"items": ["bread", "butter"]}` → "Added bread and butter to the shopping list."
3. "What's on the shopping list?" → `list.read {}` → "The shopping list has 2 items: milk and eggs."
4. "Read my to-do list." → `list.read {"list": "to-do", "personal": true}` → "Your to-do list has 1 item: renew passport."
5. Ambiguous: "Add it to the list." with no earlier item → ask "What should I add?" before calling.
6. Follow-up: after example 3, "take the eggs off" → `list.remove {"items": ["eggs"]}` → "Removed eggs from the shopping list."
7. "Clear the shopping list." → `list.remove {"clear": true}` → "Cleared the shopping list."

## Failures

| Code / situation | Say |
|---|---|
| `invalid_args` full list / too many lists | Say the message plainly; suggest clearing items in the beepboop app. |
| `status: none_matched` on remove | Say the `speak` line ("Bananas isn't on the shopping list."). |
| "There's no … list" on read | Say it; offer to start that list. |
| `reauth` | Not used here; if seen, "Lists need attention in the beepboop app." |
| `status: pending_approval` | Say nothing extra; the device already asked. Do not retry or ask for a spoken yes. |

## Speaking the result

- After add or remove, one short confirmation naming the items; do not read the whole list back.
- Reading: count first, then up to ten items in stored order, then "and N more". Empty list: "The shopping list is empty."
- Say "your" for personal lists. No markdown, bullets, emoji or numbering.

## Notes

- Shared lists are visible to everyone granted `list.read`; children have lists by default, guests do not.
- Up to 200 items per list and 30 lists per scope. Who added an item is not recorded.
