# The weather feed (`wx.json`) — contract & gotchas

The native weather card (`tiles.weather_tile` → `renderWeather`/`weatherCardHtml`)
is fed by **one external JSON document**, `GET {weather_base}/wx.json`. This page
exists so nobody has to re-discover the feed's shape by guessing — a mistake this
repo has already paid for (a since-removed `hourlyTemps` key that the feed never
had silently blanked the temp chart for weeks; see `_weather_spark`).

**If you're about to read a field off the feed, it must appear in the inventory
below.** If it doesn't, the feed does not provide it — do not guess a key name.

## Where the feed lives / how to read it

- `weather_base` is set in the operator's **private** `config.json` (never in this
  public repo — it's a LAN URL, and LAN addresses stay out of the repo). It is not
  on your dev checkout either.
- The feed is a **separate service** from family-hub — a different port on the LAN.
  The producer is the operator's own weather script (a WeatherFlow Tempest station
  plus derived almanac fields). The operator keeps the real host/port in the
  private deploy notes.
- To see the live feed during development, read `weather_base` out of the deploy
  box's config and curl it (no address hardcoded here):

  ```sh
  ssh <deploy-box> 'cd <app-dir> && curl -s \
    "$(python3 -c "import json;print(json.load(open(\"config.json\"))[\"weather_base\"])")/wx.json"' \
    | python3 -m json.tool
  ```

- The family-hub endpoint `/api/tiles/weather` returns the **trimmed** shape
  (only the keys `weather_tile` copies), not the raw feed — so it is NOT the place
  to look for fields the card doesn't yet use.

## What the feed provides (verified 2026-08-24)

It is a **single-station, real-time** feed. Ranges beyond "now" are limited:

| Horizon | What exists | Field(s) |
|---|---|---|
| Now | current conditions | `temp` `tempUnit` `feelsLike` `feelsDesc` `conditions` `humidity` `dewPoint` `uvIndex` `uvDesc` `aqi` `aqiCategory` `windSpd`/`windGust`/`windCardinal` `slp` `rainToday`/`rainRate` |
| Today | observed + forecast high/low | `obsLow`/`obsHigh` (observed so far), `fcLow`/`fcHigh` (today's forecast) |
| ±12h | hourly **temperature** curve | `tempSeries` = `{"temps": [24 hourly °, oldest→newest], "nowIndex": i}` |
| +12h | hourly **AQI** curve | `aqiForecast` = `[[epoch, aqi], …]` (12 points), plus `aqiPeak`/`aqiPeakTime`/`aqiTrend` |
| Astronomy | sun/moon | `sunrise` `sunset` `daylight` `moonPhase` `moonIllum` `moonrise` `moonset` `nextFull` `nextNew` |
| Meta | staleness/alerts | `weatherStale` `weatherAgeSec` `aqiStale` `alerts` `alertCount` |

Full key list (for reference — types/notes): `ts, station, date, time,
locationLine, temp, tempUnit, feelsLike, feelsDesc, tempTrendPerHr, temp24hDelta,
obsLow, obsLowTime, obsHigh, obsHighTime, tempSeries, fcLow, fcHigh, humidity,
dewPoint, conditions, conditionsNote, fcHour, fcWind, fcPrecipPct, fcDailyPct,
windSpd, windUnit, windAvg, windGust, windMax, windDir, windCardinal, windStatus,
slp, slpUnit, slpTrendPerHr, slpTrendDesc, slp24High, slp24Low, rainToday,
rainYest, rainMonth, rainYear, rainUnit, rainRate, rainStatus, drySpellDays,
lastRainDate, lastRainAmt, uvIndex, uvDesc, radiation, radUnit, sunrise, sunset,
sunFrac, daylight, peakSun, aqi, aqiCategory, aqiPm25, aqiForecast, aqiPeak,
aqiPeakTime, aqiForecastCat, aqiTrend, aqiTrendText, aqiStale, weatherStale,
weatherAgeSec, alerts, alertCount, alertsStale, alertsAsOf, moonPhase, moonIllum,
moonrise, moonset, nextFull, nextNew, lightning*, sager*`.

### ⚠️ There is NO multi-day (5-day / weekly) forecast in the feed

The only forward-looking data is **today's** `fcLow`/`fcHigh`, the 24h `tempSeries`,
and the 12h `aqiForecast`. There is no array of daily highs/lows for the coming
days. Any "5-day forecast" UI therefore **cannot** be backed by the feed as it
stands — the producer must add it first (next section).

## The 5-day forecast strip — the contract it waits for

The weather card renders a 5-day strip (`wxForecastHtml`) from a
`weather_tile` payload key **`forecast`**, and `weather_tile` builds that from a
feed key **`dailyForecast`**. Until the feed grows that key, the strip is absent
by design (fail-soft: fewer than 2 usable days → nothing renders, exactly like the
temp chart's <2-point rule). Nothing on the family-hub side needs to change when
the data appears — it lights up on the next poll.

**What the feed producer must add** — a `dailyForecast` array, today first, up to
~7 entries (the card shows the first 5):

```json
"dailyForecast": [
  {"day": "Mon", "hi": 81, "lo": 59, "cond": "Clear & Sunny"},
  {"day": "Tue", "hi": 84, "lo": 62, "cond": "Partly Cloudy"},
  {"day": "Wed", "hi": 78, "lo": 61, "cond": "Scattered Showers"}
]
```

- `day` — a short weekday label (`"Mon"`) or an ISO date; the card relabels the
  first entry "Today" itself. Optional (a column with no label still shows temps).
- `hi` / `lo` — daily high / low, numbers in the same unit as `temp` (`tempUnit`).
  Either may be null/absent; the card dashes the missing end and still shows the
  other. If BOTH are missing the entry is dropped.
- `cond` — the day's conditions **text** (same vocabulary as `conditions`). The
  card maps it to a drawn glyph via `wxCondKey` (clear/partly/cloudy/rain/storm/
  snow/fog; unknown → partly). Optional → a plain cloud glyph.

For a WeatherFlow Tempest producer this maps directly from the
`better_forecast` endpoint's `forecast.daily[]`
(`day_start_local`→`day`, `air_temp_high`→`hi`, `air_temp_low`→`lo`,
`conditions`→`cond`).

`tiles._weather_forecast` accepts a few obvious aliases too (`high`/`low`,
`conditions`) so a slightly different producer shape still works, but the names
above are the contract — emit those.
