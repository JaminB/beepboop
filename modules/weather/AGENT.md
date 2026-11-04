# weather

Current conditions and the coming days' forecast at home, from the weather source Home Assistant provides. Tools: `weather.current`, `weather.forecast`. Both are read-only, need no account, and report numbers in the speaker's units.

## When to use

- `weather.current` — "what's the weather", "what's it like outside", "is it cold outside", "how hot is it", "do I need a jacket", "is it raining right now", "how windy is it".
- `weather.forecast` — "what's the weather tomorrow", "will it rain this weekend", "forecast for the week", "do I need an umbrella tomorrow", "what's Friday looking like", "will it be warm enough for the beach on Saturday".
- "Should I take an umbrella" with no day → `weather.forecast` with no args (today and the next two days).

## When NOT to use

- Indoor temperatures or sensors ("temperature in the bedroom", "is the window open") → `home.state`.
- Other cities ("weather in Paris") → not supported; say you only know the weather at home.
- Past weather ("how cold was it yesterday") → not available; say so.
- Air quality, pollen, tides, sunrise → not provided by this module; say so.

## Arguments

`weather.current`: no arguments.

`weather.forecast`
- `date`: the first day asked about, `YYYY-MM-DD` in the household's time zone. "Tomorrow" → today + 1. A weekday name → the next such day (today if it is that day). "This weekend" → the coming Saturday with `days: 2`.
- `days`: 1 for a single day, 2 for a weekend, 7 for "the week". Omit both for "the next few days".
- Never ask the user which day if they gave none; use the defaults.

## Examples

1. "What's the weather?" → `weather.current {}` → cloudy, 12.5 °C, feels like 10.2 → "It's 13 degrees and cloudy, feels like 10."
2. "What's the weather tomorrow?" → `weather.forecast {"date": "2026-11-02", "days": 1}` → "Tomorrow: rainy, high 11, low 6, 80 percent chance of rain."
3. "Will it rain this weekend?" → `weather.forecast {"date": "2026-11-07", "days": 2}` → only Saturday in range → "Saturday looks rainy, 70 percent chance."
4. "Do I need a jacket?" → `weather.current {}` → "It's 13 degrees and cloudy, so yes, a light jacket."
5. Ambiguous: "What's it going to be like?" with no day → `weather.forecast {}` → summarise today and the next two days in one or two sentences.
6. Follow-up: after example 2, "and the day after?" → `weather.forecast {"date": "2026-11-03", "days": 1}`.

## Failures

| Code / situation | Say |
|---|---|
| `unavailable` "can't be reached" | "I can't reach the internet to get the weather right now." |
| `unavailable` "isn't providing a forecast" | "I can only see the current weather right now," then offer `weather.current`. |
| `not_found` "no weather source" | "Weather isn't set up yet. An admin can share a weather source in the beepboop app." |
| `not_found` "doesn't reach that day" | "The forecast only goes a week ahead." |
| `reauth` | Not used here; HA holds the provider. If seen, "Weather needs attention in the beepboop app." |
| `status: pending_approval` | Say nothing extra; the device already asked. Do not retry or ask for a spoken yes. |

## Speaking the result

- Lead with the answer to the question asked: "Yes, take an umbrella: 80 percent chance of rain tomorrow."
- Whole degrees, no unit word beyond "degrees"; the numbers are already in the speaker's units. Wind only if asked or above about 30 km/h (20 mph). Humidity only if asked.
- Rain chance under 20 percent: say "dry" or say nothing. Read at most three days; for a week, summarise the trend ("dry and warming until Friday, rain on Saturday").
- One or two sentences. No markdown, lists, emoji or URLs.

## Notes

- The forecast covers about a week from today; earlier days are gone.
- `condition` values are Home Assistant's (`partlycloudy`, `clear-night`, `snowy-rainy`); say them as plain words.
