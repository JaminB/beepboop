# Setting up Lists

> Scope: household · Time: none · You need: nothing

No setup required. The shared shopping list and everyone's personal lists work as soon as beepboop is running, with or without an internet connection.

## 1. Prerequisites

- None. Shared lists belong to the household; "my" lists belong to the person whose voice beepboop recognised.

## 2. Get credentials

- None. Lists use no accounts or API keys, and are not synced to Home Assistant's to-do lists in v0.1.

## 3. Connect in beepboop

1. Nothing to connect. Integrations → Lists shows *ok*.
2. Say "Hey beepboop, add milk to the shopping list" to try it.

## 4. Grant access

`list.add` and `list.remove` are `act` tier; `list.read` is `read`. Admin, Member and Child presets include `list.*`. The Guest preset does not: a visitor cannot read or change the shopping list unless an admin adds `list.read` (Guest access does this for the evening) or `list.*` on People › Guest.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "There's no hardware store list" | That list has never had an item | Add something to it first; lists are created on first use |
| An item went to the shared list instead of mine | You said "the list", not "my list" | Say "my to-do list" for personal lists |
| beepboop refuses list requests from a visitor | Guest has no list grants | Use Guest access or add `list.read` for the Guest profile |
| "The shopping list is full" | 200-item limit | "Clear the shopping list" or remove items |

## 6. Privacy

- What leaves the house: nothing. Lists never use the network. Your spoken request goes to the configured LLM only when the fast path does not handle it.
- What is stored: the item text and when it was added. Shared lists do not record who added an item. Personal lists are stored under your profile.
- Delete: remove items or "clear the list" by voice. Deleting your profile deletes your personal lists; shared lists stay with the household.
