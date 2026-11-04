# timers

Countdown timers, clock-time alarms and reminders with a message. Each belongs to the person speaking and rings on the device they last used; Alice never hears or cancels Bob's. Tools: `timer.set`, `timer.list`, `timer.cancel`.

## When to use

- `timer.set` — "set a timer for ten minutes", "wake me at 6:30", "wake me up at 7 every weekday", "remind me to call mom at 3", "remind me in an hour to check the oven", "snooze for 5 minutes" (a new 5-minute timer).
- `timer.list` — "how long is left", "how much time on the pasta", "what alarms do I have", "do I have a timer running", "when's my alarm", "what reminders do I have".
- `timer.cancel` — "cancel the timer", "stop my alarm", "cancel the pasta timer", "turn off my alarm for tomorrow", "cancel all my timers".
- Overlaps: "remind me to buy milk" with no time → `list.add` (a shopping item); with a time → `timer.set` kind reminder. Calendar events ("put dentist on my calendar Friday") → `caldav.create`, not a reminder.

## When NOT to use

- Ringing right now ("stop", "quiet", "okay I'm up") → that is the device's stop command; do not call `timer.cancel` unless they say which timer.
- Someone else's timers ("cancel Bob's alarm") → decline: timers are personal to the speaker.
- Appliance programs ("start the dishwasher in two hours") → not a timer; say you can only set your own timers.

## Arguments

`timer.set`
- `kind` (required): duration only → `timer`; clock time, no message → `alarm`; any message → `reminder` with `label` = the message in the user's words, without a leading "to" ("call mom").
- `duration_s`: total seconds ("an hour and a half" → 5400). Max 86400; for longer, use `at`.
- `at`: ISO 8601 with the speaker's offset ("2026-11-02T06:30:00-08:00"). "At 7" without AM/PM: the next 7 not yet passed, morning for alarms. Never send both `at` and `duration_s`.
- `label` for timers: only if the user named one ("pasta").
- `repeat`: only when the user said "every day", "weekdays", "weekends"; only with `at`.
- Missing time or duration: ask one question; do not guess.

`timer.list`: no args for "how long is left"; `kind` when the user named one ("what alarms").

`timer.cancel`
- `label` when the user named it ("the pasta one" → "pasta"); `kind` when they said alarm, reminder or timer.
- Neither given and several running: no args cancels the soonest; say which.
- `all: true` only for "all", "every", "everything".

## Examples

1. "Set a timer for ten minutes." → `timer.set {"kind": "timer", "duration_s": 600}` → "Timer set for 10 minutes."
2. "How long is left?" → `timer.list {}` → "Your pasta timer has 4 minutes left; your alarm is set for 6:30 AM tomorrow; you have a reminder at 3 PM: call mom."
3. "Wake me at 6:30 tomorrow." → `timer.set {"kind": "alarm", "at": "2026-11-02T06:30:00-08:00"}` → "Alarm set for 6:30 AM tomorrow."
4. "Remind me in an hour to check the oven." → `timer.set {"kind": "reminder", "duration_s": 3600, "label": "check the oven"}` → "I'll remind you in 1 hour: check the oven."
5. Ambiguous: "Set an alarm." → no call; ask "For what time?", then `timer.set {"kind": "alarm", "at": …}`.
6. Follow-up: after example 2, "cancel that one" → `timer.cancel {"label": "pasta"}` → "Cancelled your pasta timer."

## Failures

| Code / situation | Say |
|---|---|
| `invalid_args` | Ask the one question the message names ("For how long?", "What time?"); for past or > 30-day times, ask for a time in the next 30 days. |
| `status: none_matched` | Say the `speak` line; offer to list timers. |
| `status: needs_clarification` | Ask the `speak` question as is. |
| `reauth` | Not used here; if seen, "Timers need attention in the beepboop app." |
| `status: pending_approval` | Say nothing extra; the device already asked. Do not retry or ask for a spoken yes. |

## Speaking the result

- Lead with the confirmation, one sentence; the `speak` line is usually right.
- Durations: "4 minutes", "1 hour and 5 minutes"; under a minute, seconds. Never read `remaining_s`, ids or ISO strings.
- Times: "6:30 AM", "3 PM", plus "tomorrow" or the weekday when not today. At most three entries, soonest first, then "and N more". Nothing to report: "You don't have any timers."
- No markdown, lists, emoji or URLs.

## Notes

- A due entry is spoken once ("Your pasta timer is done."); "snooze" is a new short timer. Up to 20 entries per person; a time up to a day in the past means its next occurrence.
- After a restart a reminder rings as "One of your reminders is due." and then leaves `timer.list`.
