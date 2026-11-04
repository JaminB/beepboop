# Setting up Timers

> Scope: household · Time: none · You need: nothing

No setup required. Timers, alarms and reminders work as soon as beepboop is running, with or without an internet connection or Home Assistant.

## 1. Prerequisites

- None. Each person's timers are their own; beepboop knows whose they are from the voice that set them.

## 2. Get credentials

- None. Timers use no accounts or API keys.

## 3. Connect in beepboop

1. Nothing to connect. Integrations → Timers shows *ok* and the number of pending entries.
2. Say "Hey beepboop, set a timer for ten minutes" to try it.

## 4. Grant access

`timer.set` and `timer.cancel` are `act` tier; `timer.list` is `read`. Every preset includes `timer.*`, including Child and Guest, so a visitor can set a pasta timer. To take timers away from someone, remove `timer.*` on their Access tab.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A timer rang on the wrong speaker | It rings on the device the person last spoke to | Ask from the room you want it to ring in |
| "One of your reminders is due" with no message | beepboop restarted between setting and ringing | Ask again with "what reminders do I have"; set it again if needed |
| A timer did not ring | The device was off or restarting for more than 5 minutes when it was due | Late timers are skipped rather than rung long after; set it again |
| "You already have 20 timers" | Per-person limit | Cancel some ("cancel all my timers") |

## 6. Privacy

- What leaves the house: nothing. Timers never use the network. Your spoken request goes to the configured LLM only when the fast path does not handle it.
- What is stored: per person, each entry's kind, due time, repeat rule and label, in beepboop's database. A household list of due times and owners (without labels) lets timers ring after a restart.
- Delete: cancel by voice, or delete your profile, which removes your entries; entries are also removed once they ring.
