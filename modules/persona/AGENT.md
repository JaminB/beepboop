# persona

Changes how the assistant talks to the person speaking: speaking speed and answer length. One tool: `persona.set`. Changes apply only to the speaker and last until they change them back; the household's name and personality are set by an admin in the app.

## When to use

- `persona.set` — "speak slower", "talk faster", "slow down a bit", "you're talking too fast", "be more brief", "keep your answers short", "you can give me more detail", "be chattier", "go back to normal", "talk normally again".
- Speed and length in one request ("slower and shorter please") → one call with both arguments.

## When NOT to use

- "Louder" / "quieter" → `media.control` volume on the speaker, not persona.
- Renaming the assistant, changing its personality or voice, or changing someone else's settings → say an admin or the person can do that in the beepboop app (Integrations › persona).
- "Stop" / "be quiet" while speaking → that cancels the reply; no tool.

## Arguments

- `speech_rate`: "slower" or "faster" moves one step (10 percent) within 0.7–1.4; "normal" drops the speaker's speed change.
- `verbosity`: "brief" for short answers, "chatty" for more detail, "normal" for the household default.
- `reset: true` for "go back to normal" or "undo all that"; then omit the others.
- "Slower" with no hint whether speech or answers: it means speech. "Shorter" means verbosity brief.
- Nothing recognisable ("change how you talk") → ask "Slower, faster, or shorter answers?".

## Examples

1. "Speak slower." → `persona.set {"speech_rate": "slower"}` → "Okay, I'll speak slower."
2. "Keep your answers short." → `persona.set {"verbosity": "brief"}` → "Okay, I'll keep it brief."
3. "Slower, and shorter answers please." → `persona.set {"speech_rate": "slower", "verbosity": "brief"}` → "Okay, I'll speak slower and I'll keep it brief."
4. "Go back to normal." → `persona.set {"reset": true}` → "Okay, back to the household defaults."
5. Ambiguous: "Talk differently." → no call; ask "Slower, faster, or shorter answers?".
6. Follow-up: after example 1, "even slower" → `persona.set {"speech_rate": "slower"}`; at the limit → "Okay, that's as slow as I go."

## Failures

| Code / situation | Say |
|---|---|
| `invalid_args` | Ask "Slower, faster, or shorter answers?" |
| Denied (`not_granted`) | The device already explained; add nothing. |
| `reauth` | Not used here; if seen, "Persona settings need attention in the beepboop app." |
| `status: pending_approval` | Say nothing extra; the device already asked. Do not retry or ask for a spoken yes. |

## Speaking the result

- Confirm in the new style straight away, one short sentence; the `speak` line is right.
- Never read numbers like 0.9 or the voice id.
- No markdown, lists, emoji or URLs.

## Notes

- Only the speaker is changed. A guest or an unrecognised voice cannot change settings.
- A new speed applies from the next reply.
