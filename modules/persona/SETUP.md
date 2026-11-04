# Setting up Persona

> Scope: household (with per-person choices) · Time: ~2 min · You need: nothing

The persona decides how beepboop talks: the name the household uses for it, its personality, and, for each person, the voice, speaking speed and answer length. It works out of the box with sensible defaults.

## 1. Prerequisites

- None. Voices come from the text-to-speech voices installed on the device (System › Voice).

## 2. Get credentials

- None. The persona uses no accounts or API keys.

## 3. Connect in beepboop

1. Open the beepboop app → **Integrations → persona**.
2. Household defaults (admins): **Assistant name**, **Personality**, **Answer length**, **Voice**, **Speaking speed**. The wake word stays "Hey beepboop" whatever the name.
3. *Your settings* (everyone): override **Answer length**, **Voice** and **Speaking speed** for yourself, or keep "use household default".
4. Press **Save**. Health shows *ok*. Changes apply from the next reply.
5. By voice, anyone recognised can say "speak slower", "be more brief" or "go back to normal"; this changes only their own settings.

## 4. Grant access

`persona.set` is `act` tier. The Admin and Member presets include it. The Child and Guest presets do not, so children and visitors keep the settings an adult chose; add `persona.*` on a child's Access tab to let them change their own.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Speak slower" changed nothing | Already at the slowest step, or the request came from another recognised person | Check *Your settings*; re-enrol your voice if beepboop often mistakes you |
| A child cannot change the speed | Child preset has no `persona.*` | Add it on the child's Access tab, or set their speed under *Your settings* for them |
| Answers feel too long for everyone | Household answer length | Set **Answer length** to Brief in the household defaults |

## 6. Privacy

- What leaves the house: the persona text (name, personality and answer-length guidance) is part of the instructions sent to the configured LLM with each request. Nothing else.
- What is stored: household settings, and each person's own voice-made changes under their profile.
- Delete: "go back to normal" by voice removes your changes; deleting your profile removes them too.
