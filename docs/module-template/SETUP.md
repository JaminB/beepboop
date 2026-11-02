# Setting up Parcel tracking

> Scope: per person · Time: ~5 min · You need: an account with the carrier aggregator (fictional in this template)

Parcel tracking is personal: each household member connects their own carrier account and tracks their own parcels. Nobody else in the house can see them, and Child and Guest profiles cannot use it.

## 1. Prerequisites

- A beepboop login (People → you). Members and Admins only.
- An account with the carrier aggregator and a developer API key. Free tiers usually allow a few hundred lookups a day; beepboop only looks a parcel up when you ask about it.

## 2. Get credentials

1. Sign in to the aggregator's developer console.
2. Open **API keys → Create key**. Name it `beepboop`.
3. Choose the **read-only / tracking** permission if the console offers one. beepboop never needs write access to your carrier account.
4. Copy the key. It is shown once.

## 3. Connect in beepboop

1. Open the beepboop app → **Integrations → Parcel → Your connection**.
2. Paste the key into **Carrier API key**. Optionally set **Default carrier** (leave on *Detect automatically* unless detection fails).
3. Press **Test and save**. beepboop stores the key encrypted on the Pi (shown as `•••••` afterwards) and immediately runs a health check; you should see *ok · Signed in to the carrier aggregator*. If you see *reauth*, the key was rejected: create a new one and paste it again.
4. Say "Hey beepboop, track package …", or add a tracking number on the same page under **Tracked parcels**.

## 4. Grant access

The Parcel tools are `personal` tier. The Member and Admin presets include them. If an admin customized grants, they need `parcel.*` at `personal` on your Access tab. Because `personal` requests are about your own data, beepboop wants to be sure it is you: when your voice match is not confident enough it asks for a quick OK on your phone instead of guessing, and the request continues as soon as you approve.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Your parcel tracking needs reconnecting" | Key expired or revoked | Create a new key and paste it (step 3) |
| "The carrier doesn't know that number yet" | Label created but not scanned | Wait a day and ask again |
| Phone approval asked before every parcel request | Voice match below the personal threshold | Re-enrol your voice (People → you → Voice); check the household sentence on System › Voice |
| Parcel tools missing for you | Child or Guest profile, or grants removed | Parcel tracking is only for people beepboop recognises with a Member or Admin profile; ask an admin to check Access |
| Wrong carrier detected | Ambiguous number format | Set **Default carrier**, or say the carrier when adding |

## 6. Privacy

- What leaves the house: tracking numbers you add, sent to `api.example-carrier.test` over HTTPS (the module's only `egress` destination); your spoken request text, sent to the configured LLM provider. Carrier status text comes back and is stored on the Pi.
- What is stored: your API key (encrypted), tracking numbers, labels, last known status. Nothing about other people's parcels.
- Delete: remove individual parcels by voice or in the app; **Disconnect** on the integration page deletes the key and every parcel. Deleting your profile removes everything.
