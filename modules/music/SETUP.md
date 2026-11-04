# Setting up Music
> Scope: household (each person may pick a default speaker) · Time: ~15 min · You need: the `music` install profile, speakers Music Assistant can reach (AirPlay, Chromecast, Sonos, Snapcast), and a music source

beepboop plays music through Music Assistant (MA), which runs next to Home Assistant on the Pi. beepboop never talks to the speakers or streaming services itself; it asks Home Assistant to tell MA what to play.

## 1. Prerequisites

- The optional `music` compose profile enabled at install (see the install guide); it starts the MA container.
- At least one speaker on the same network as the Pi.
- A music source configured in MA: a local library, internet radio, or a streaming account MA supports.
- On a 4 GB Pi, `music` is the one optional profile you can enable; choose 8 GB if you also want Zigbee, Z-Wave or Matter companions.

## 2. Get credentials

None for beepboop. Streaming accounts (Spotify, Tidal and others) are signed in inside MA's own interface, which an admin reaches through the gated admin address described in the operations guide. beepboop never sees those passwords or tokens.

## 3. Connect in beepboop

1. As an admin, open Integrations › music and press **Set up**. beepboop adds MA to Home Assistant for you.
2. MA's speakers appear as new devices. Approve them in the new-device inbox (Integrations › ha › New devices) and grant them by room, like any other device.
3. Pick the household **Default speaker**: where music plays when nobody names a room.
4. Each person can choose their own default speaker on the same page ("Your settings"), for example the bedroom speaker for a teenager.
5. Health shows how many MA speakers are available. "Degraded" means none is granted yet.

## 4. Grant access

`music.play` is an `act` tool: a recognised voice is enough. The Admin and Member presets include it; the Child and Guest presets do not, so an admin adds `music.*` for a child in People › <name> › Access (it stays within the child's `act` ceiling). `music.now_playing` is `read`. Neither ever needs a phone approval on its own; after a `music.now_playing` answer, other actions in the same request do, because track titles are outside text. For the built-in Guest profile the same is done in People › Guest › Access.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "No speakers are set up for music yet" | No MA speaker is granted to that person | Grant the speakers in Integrations › ha › New devices or People › <name> › Access |
| "Which speaker?" every time | No default speaker and several speakers granted | Set a household or personal default speaker |
| "That speaker is offline" | The speaker is off or MA lost it | Power-cycle the speaker; check it in MA |
| "Music Assistant couldn't play that" | No source has it, or the account is signed out | Check the music source in MA; try a different name |
| Music plays on the TV, not the speaker | Both are named after the room | Name the room more specifically; MA speakers are preferred |

## 6. Privacy

- What leaves the house: nothing from beepboop. MA contacts the streaming services you configured in MA, under MA's own settings.
- What is stored: the default-speaker choices (settings, plain text) and each request in Activity with arguments redacted. No listening history is kept by beepboop.
- How to delete: remove a personal default speaker on Integrations › music; disable the module on the same page; streaming accounts are removed in MA.
