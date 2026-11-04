# ha

Controls and reads the home through Home Assistant. Five tools, always loaded: `home.turn_on`, `home.turn_off`, `home.set`, `home.state` (read) and `media.control`. You pass words, never entity ids: the server resolves names against only the devices this person is allowed to use, so a device you cannot see simply does not match.

## When to use

- `home.turn_on` — "turn on the kitchen lights", "lights on", "switch on the fan", "open the blinds", "lock the front door", "activate the movie scene", "start the vacuum".
- `home.turn_off` — "turn off the lights", "kill the lights in the kitchen", "everything off in the living room", "close the garage", "unlock the back door", "send the vacuum home".
- `home.set` — "dim the bedroom lamp to 30 percent", "a bit brighter", "make the lights warm white", "set the thermostat to 21", "two degrees warmer", "put the heating on cool", "open the blinds halfway", "fan to half".
- `home.state` — "is the front door locked", "are any lights on", "temperature in the bedroom", "what's on in the house" (no target: a snapshot).
- `media.control` — "pause", "skip this", "next song", "go back", "stop the music", "mute the tv", "volume down in the kitchen", "set the volume to 40".
- Follow-ups ("and the hallway", "the ceiling one") → the same tool with the new name.

## When NOT to use

- Choosing what to play ("play some jazz", "play my liked songs") → `music.play`, then use `media.control` for pause, skip and volume.
- Timers, alarms and reminders → `timer.set`; never HA timers.
- Shopping or to-do items → `list.add`.
- Forecasts → the weather tools; `home.state` is for the household's own sensors.
- Never invent a device. If a name does not resolve, say you couldn't find it; do not retry with a guessed name.

## Arguments

`target` (all five tools; optional for `home.state` and `media.control`)
- `name`: the words the person used, minus the verb: "kitchen lights", "the lamp", "tv". Keep the room in the name if they said it together ("kitchen lights"); the server splits it.
- `area`: only when the room was said separately ("in the kitchen"). Floors work too ("upstairs").
- `domain`: only when the name is generic ("turn on the kitchen" → `area: kitchen`, `domain: light` if they mean lights).
- `all: true` for "everything", "all the lights", "every speaker"; it needs `area` or `domain`.

`home.turn_on` / `home.turn_off`
- Locks: `home.turn_on` locks, `home.turn_off` unlocks. Covers: on opens, off closes. Vacuums: on starts, off sends it home. Alarm panels: on arms, off disarms.
- Unlocking, disarming, garage doors, gates, scenes and scripts usually need the person's approval; call normally and let the approval flow happen.

`home.set`
- One `attribute` per call: `brightness` (0-100), `color` (a name like "red" or a hex), `color_temp` (kelvin, e.g. 2700 warm, 5000 cool), `temperature` (household unit), `hvac_mode` (heat, cool, auto, heat_cool, dry, fan_only, off), `fan_speed` (0-100), `position` (0-100, 100 = open), `volume` (0-100), `preset` (a mode name).
- Use `value` for absolute and `delta` for relative ("dimmer" → `delta: -20`, "a bit brighter" → `delta: 20`, "two degrees warmer" → `delta: 2`). Never both.
- Thermostat changes are clamped to 5 degrees per call.

`home.state`
- No `target` gives a short snapshot of the home grouped by room.
- `attribute` narrows the answer ("battery", "humidity", "temperature").

`media.control`
- Omit `target` for "pause" or "next": the server uses the player that is playing.
- `volume` only with `action: volume_set`.

## Examples

1. "Turn off the kitchen lights." → `home.turn_off {"target": {"name": "kitchen lights"}}` → `{"status": "ok", "affected": ["Kitchen ceiling", "Counter lights"]}` → "Kitchen lights are off."
2. "Dim the bedroom lamp to 30 percent." → `home.set {"target": {"name": "bedroom lamp"}, "attribute": "brightness", "value": 30}` → "Bedroom lamp set to 30%."
3. "Is the front door locked?" → `home.state {"target": {"name": "front door"}}` → `{"entities": [{"name": "Front door", "state": "locked"}]}` → "Yes, the front door is locked."
4. "Unlock the front door." → `home.turn_off {"target": {"name": "front door"}}` → `{"status": "pending_approval"}` → say nothing extra; the device already asked for approval.
5. Ambiguous: "Turn on the lamp." → `home.turn_on {"target": {"name": "lamp"}}` → `{"status": "needs_clarification", "speak": "Which one do you mean: Bedroom lamp or Living room lamp?"}` → ask exactly that; then "the bedroom one" → `home.turn_on {"target": {"name": "bedroom lamp"}}`.
6. "Two degrees warmer in the living room." → `home.set {"target": {"area": "living room", "domain": "climate"}, "attribute": "temperature", "delta": 2}` → "The living room is set to 23 degrees."

## Failures

| Code / situation | Say |
|---|---|
| `status: needs_clarification` | Ask the one question in `speak`, listing at most three names. |
| `status: none_matched` | "I couldn't find <what they said>." Do not guess another device. |
| `status: unsupported` | Say the device can't do that ("The fan can't change color."). |
| `status: nothing_playing` | "Nothing is playing." |
| `status: partial` | Report what worked, then what didn't respond. |
| `unavailable` | "I can't reach the home right now." `home.state` may still answer with `stale: true`; say the values are the last known ones. |
| `not_granted` / policy denial | The device layer speaks the denial; add nothing. |
| `status: pending_approval` | Say nothing extra; do not retry or ask for a spoken yes. |
| `invalid_args` | Fix the argument the message names once, or ask the person. |

## Speaking the result

- Use `speak` when it fits; otherwise one short sentence. Confirm with the device name and the new state ("Kitchen lights are off.").
- More than three devices: give the count ("Six lights are off."), not the list.
- Say "30 percent" and "21 degrees".
- Snapshot: one or two sentences on what is on, open or unlocked.
- No markdown, lists or entity ids.

## Notes

- You only ever see this person's devices. A device that exists but is not theirs reads as "couldn't find" for guests and as a denial for everyone else; never speculate about devices you were not shown.
- New devices are invisible until an admin sorts them in the new-device inbox; if someone asks for a device that was just added, suggest an admin check Integrations › Home Assistant › New devices.
- Children are capped at 70 percent volume by the server.
