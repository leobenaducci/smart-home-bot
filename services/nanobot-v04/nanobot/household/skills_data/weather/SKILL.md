---
name: weather
description: "Invoke with JSON: {\"skill\":\"weather\",\"action\":\"...\"}. Current weather and forecasts. Actions: get_weather([location],[units]) | get_forecast([location],[days],[units]). Leave location out for the household's own weather; never ask which city. Use when a message is about the weather, rain, temperature or the forecast."
homepage: https://wttr.in/:help
# `always: true`: the body below is in every prompt, so this description
# carries triggers rather than the API -- the body and the skill's own code
# already hold that. Measured 2026-09-10: every model that fumbled this
# skill called it as a function (`unknown:<name>`) because the invocation
# convention was in a file it never read. If demoted to on-demand, the
# description has to grow back.
#
# The body used to end with a "By hand" section of `curl wttr.in` commands,
# which put two ways of getting the weather into every prompt -- and the
# description said "always include location or ask" while the body said
# never ask. Measured 2026-09-11: DeepSeek V4 Flash took the curl route and
# spent its whole turn adjusting wttr.in format strings, four calls, no answer.
# The skill's own code already works out the place and asks wttr.in itself;
# the model only needs the one route.
metadata: {"nanobot":{"emoji":"🌤️","translatable":true,"requires":{"bins":["curl"]},"always":true}}
---

# Weather

`location` is optional. Left out, you get the household's own weather -- the
skill works that out itself and never wakes a phone. Pass `location` only when
the person names somewhere else, and never ask them which city.

```json
{"skill": "weather", "action": "get_weather"}
{"skill": "weather", "action": "get_forecast", "days": 3}
{"skill": "weather", "action": "get_forecast", "location": "Madrid"}
```

`get_conditions` and `get_current_weather` are the same as `get_weather`.

Call it through the block above, never with curl or exec: the skill already
knows where the household is. Answer in the language the person wrote in,
whatever language the forecast comes back in.
