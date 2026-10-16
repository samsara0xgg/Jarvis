# ADR 0285 — The weather comes only from Apple WeatherKit

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** 0282

## Context

- Allen decided the brain's weather should be Apple Weather's, because it is better: it has
  minute-by-minute precipitation for the next hour ("几点开始下雨") and official alerts
  (Environment Canada in Victoria). He then said two sources are not wanted ("不用整两个吧"):
  Open-Meteo's weather is retired, with no fallback.
- ADR 0282 made `weather` one tool for any place and day. That shape stays: the model should
  answer any weather question in one correct call.
- WeatherKit's REST API (`weatherkit.apple.com/api/v1/weather/{language}/{lat}/{lng}`) needs an
  ES256 JWT: header `alg`, `kid` (the key's id) and `id` (`<team id>.<services id>`); claims
  `iss` (team id), `sub` (services id), `iat`, `exp`. It takes no place name, and the day
  boundaries follow the `timezone` parameter; alerts need `countryCode`. It has no air quality.
  The daily forecast covers today and nine days after it; this ADR does not rely on past days.
- Open-Meteo's geocoding answers a time zone and a country code with each match; its air
  quality API is one small call that needs no key.
- The key does not exist yet; the owner creates it in the Apple developer portal.

## Decision

`weather` takes its weather only from WeatherKit, with the same name and arguments (`place`,
`start_date`, `end_date`, `hourly`, `air_quality`), and every answer carries Apple's attribution.

- One request per answer asks for the current conditions, hourly and daily forecasts, the next
  hour's minutes and alerts. The answer keeps the old field names where WeatherKit has the value,
  and adds `next_hour` (dry or raining now, `starts_at`, `stops_at`, peak intensity, or why there
  is none for this place), `alerts` (headline, severity, area, effective, expires, source;
  an empty list means none) and `attribution`.
- Dates run from today to 9 days ahead in the place's own calendar; earlier or later is an error
  that names the range and sends the model to `web_search`.
- A named place is geocoded by Open-Meteo, which also gives its zone and country. A point
  (`here` or `lat,lng`) uses the zone and country of a place the lookup listed, or home's within
  100 km of home; any other takes its zone from tzfpy's offline map. A place with no country
  (home without `home.weather.country_code`, or such a point) takes its zone's country from the
  system tz database's `zone.tab`. Alerts are read only where a country is known.
- An answer is held 5 minutes by request, so the home's weather line and a turn's call for the
  same place cost one request. The home line is built from the same answer.
- Without a usable `weather.weatherkit` key the tool is still offered and answers "weather is
  not set up: WeatherKit key missing"; the home shows no weather; the boot logs one line saying so.
  There is no other weather source to fall to.
- The key file is read only to sign. The key, the token and the request headers are never logged,
  returned or put in an error; a 401 is reported as its status and Apple's own reason word.
- Air quality and place names stay on Open-Meteo.

## Alternatives rejected

- **Keep Open-Meteo as the fallback when there is no key.** Allen chose one source; two paths
  mean two sets of fields the model sees and a test matrix for each. Deploying only after the key
  is on the brain costs nothing, and until then the tool says why it is empty.
- **PyJWT for the token.** It is installed only as a dependency of `mcp`; the repo already
  signs ES256 with `cryptography` for APNs (ADR 0210), so the same class signs this token and no
  dependency is added.
- **Guess a point's zone from its longitude.** A wrong zone moves every day boundary and every
  sunrise silently; an error that asks for a name is wrong never.
- **Ask WeatherKit for alerts without a country.** The API returns none then, which would read as
  "no warnings"; the result says alerts are unavailable instead.

## Consequences

- Days before today are no longer available (ADR 0282 had 7 back) and the horizon is 9 days, not
  16; the daily rows lose `feels_*_c` and `gust_max_kmh`, which WeatherKit does not give.
- A WeatherKit outage or a revoked key leaves the weather empty, with no fallback.
- Weather requests go to Apple with the coordinates; Apple Weather must be named wherever the
  data is shown, so the phone page that shows weather must show `attribution` when it does.
- WeatherKit is free to 500,000 calls a month; at 5 minutes per request this is far from it.
