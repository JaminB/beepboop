# Setting up Weather

> Scope: household · Time: ~3 min · You need: a weather integration in Home Assistant (Met.no is set up by default)

beepboop reads the weather from Home Assistant, so there is no weather account or API key to manage. Home Assistant's default integration, Met.no (the Norwegian Meteorological Institute), needs only the home location you set during onboarding.

## 1. Prerequisites

- Home Assistant connected to beepboop (Integrations → Home Assistant shows *ok*).
- A `weather.*` entity in Home Assistant. A fresh install has `weather.forecast_home` from Met.no. Any other weather integration works too.

## 2. Get credentials

- None for Met.no. If you prefer another provider, add its integration in Home Assistant; any key it needs stays in Home Assistant, never in beepboop.

## 3. Connect in beepboop

1. Open the beepboop app → **Integrations → Weather**.
2. **Weather source**: pick the weather entity. Leave it empty to use the first weather entity shared with beepboop.
3. **Units**: the household default (metric or imperial). Each person can override it under *Your settings* on the same page.
4. Press **Test and save**. Health shows *ok · Using weather.forecast_home*. *degraded* means no weather entity is shared yet (step 4 below) or the provider is offline.

## 4. Grant access

Both tools are `read` tier, and every preset (Admin, Member, Child, Guest) includes `weather.*`. The weather entity itself must be shared: grant it to Everyone in Integrations › Home Assistant › New devices. Until then beepboop says "Weather isn't set up yet".

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Weather isn't set up yet" | No weather entity shared with beepboop | Grant the weather entity in Integrations › Home Assistant › New devices |
| "I can't reach the internet to get the weather" | The house is offline, or the provider is down; HA marks the entity unavailable | Check the internet connection; it recovers on its own |
| "I can only see the current weather right now" | This weather entity gives no forecast attribute to beepboop | Use a different weather entity, or wait for the forecast service support noted in the release notes |
| Temperatures in the wrong units for you | Household default applies | Integrations → Weather → *Your settings* → Units |

## 6. Privacy

- What leaves the house: nothing from beepboop. Home Assistant's weather integration sends the household's configured location to its provider (Met.no by default) on its own schedule, whether or not anyone asks.
- What is stored: nothing beyond the chosen entity and units setting. Weather questions appear in the activity timeline like any other request.
- Delete: remove the setting on the Integrations page; the location lives in Home Assistant's configuration.
