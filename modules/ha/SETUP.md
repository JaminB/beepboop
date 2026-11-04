# Setting up Home control

> Scope: household · Time: ~10 min · You need: an admin login and the devices you already have in Home Assistant

Home control is how beepboop turns lights on, reads the thermostat, locks doors and pauses the music. It works with Home Assistant, which beepboop runs and onboards for you on first boot; nobody in the house ever has to open Home Assistant's own app for everyday use.

## 1. Prerequisites

- beepboop installed and claimed (the Home Assistant container starts with it and is onboarded automatically).
- Your devices paired in Home Assistant. Pairing Zigbee, Z-Wave, Matter or Wi-Fi devices happens in Home Assistant's own UI: open it with **Open Home Assistant** on Integrations › Home Assistant (admins only; Home Assistant asks for its own login the first time).
- An admin login in beepboop to sort new devices.

## 2. Get credentials

Nothing to copy. beepboop created its own Home Assistant service account (`beepboop-system`) and a long-lived token during first boot, and keeps both encrypted on the Pi. If Home Assistant was set up before beepboop (an existing `/config` volume or a restore), Health shows *ha: needs login*: an admin signs in once with the Home Assistant owner account on Integrations › Home Assistant and beepboop mints its token from that session.

## 3. Connect in beepboop

1. Open Integrations › Home Assistant. Health should read *ok* with the number of devices beepboop can see.
2. If it reads *reauth*, Home Assistant rejected the token (for example after a restore onto a new Home Assistant): sign in once with the owner account as in step 2. beepboop never retries a rejected token in a loop.
3. If it reads *unavailable*, Home Assistant is not answering; beepboop reconnects on its own within 30 seconds. Voice keeps answering "is the door locked" from the last known state and says so.

## 4. Grant access

Every new device lands in Integrations › Home Assistant › New devices and is **invisible to everyone** until an admin decides who may use it:

- **Everyone** for lights, switches, media players and other everyday devices (the `act` tier).
- **Adults** for locks, alarm panels, garage doors, gates and sirens (always `critical`: beepboop asks for approval on the person's phone every time).
- **Only** specific people, or **Ignore** to hide it for good.
- Scripts, scenes, automations, helpers, device buttons and template switches can do anything, so they start at `critical`. Read what the script or scene does on the inbox card before lowering its tier.

Grants are by Home Assistant's permanent device ID, so renaming a device in Home Assistant never breaks access. Room grants ("everyone may control the living room") are on People › <name> › Access. Children and guests are capped at `act`; they can never unlock or disarm anything.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "I couldn't find the lamp" | The device is new and still in the inbox, or not granted to you | An admin sorts it in Integrations › Home Assistant › New devices |
| "I can't reach the home right now" | Home Assistant is restarting or stopped | Wait 30 s; check System › Health; `beepboop doctor` on the Pi |
| Health shows *reauth* | Home Assistant rejected beepboop's token | Sign in once with the owner account on Integrations › Home Assistant |
| "Which one do you mean" every time | Two devices share a name | Rename one in Home Assistant, or add an alias there |
| Unlocking always asks for approval | By design: locks are `critical` | Approve on your phone; voice alone never unlocks |
| A scene never runs | Scenes start at `critical` and need approval | Lower its tier in the inbox after reading what it does |

## 6. Privacy

- What leaves the house: nothing. Home Assistant runs on the Pi and is reachable only from the Pi itself; device commands never go to the internet (the LLM sees device names you are granted, never entity ids or other people's devices).
- What is stored: a mirror of Home Assistant's device list (names, rooms, permanent IDs) and who may use what; the Home Assistant token, encrypted. Device states stay in memory only.
- Delete: removing a device in Home Assistant removes it here; its grants are kept 30 days in case it comes back, then deleted.
