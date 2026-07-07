# Pool Tracking

A self-hostable web app that tracks the chemical balance of home swimming pools,
gives **AI-generated advice** on how to correct the water, charts your readings
over time, and links them to the **real weather** on the day they were taken so
you can spot what is driving changes in your pool.

## Features

- **Passwordless login.** Sign in with a one-time email link (magic link). Each
  user gets their own pools, readings and device connections. Emails are sent via
  **Resend** or SMTP, or printed to the console in local mode.
- **Manual or automatic readings.** Type in a test-kit result, or pull readings
  from your devices:
  - **Aiper HydroComm** smart pool monitor (pH, ORP, EC/TDS, temperature). Water
    quality is read from the device's AWS IoT shadow over MQTT.
  - **Blueriiot Blue Connect** probe (pH, ORP, temperature, salinity).
- **Scheduled device syncing.** Enable auto-sync per device (with a target pool
  and a per-device frequency) on the Devices page and a background scheduler pulls
  fresh readings on that cadence (falling back to `AUTO_SYNC_INTERVAL_HOURS`,
  default 1). Manual sync still works any time.
- **Read a test strip from a photo.** Upload a photo of a dipped strip next to its
  colour key and Claude reads each pad and pre-fills the form for you to confirm.
  The photo is stored with the reading.
- **Saved AI advice from Claude.** Each pool keeps one set of advice (summary,
  per-parameter recommendations with concrete **dosing**, and an ordered
  **next-steps** to-do list). It is generated when you add a reading or press
  **Refresh**, not on every page view, so it stays stable and cheap.
- **Per-pool notes.** Add free-text context ("recently shocked", "lots of leaves")
  that is fed into the advice.
- **Context log.** Record dated events across a pool's whole life (chemicals added,
  a new salt cell or pump, a resurface, a drain and refill, heavy use, a storm),
  each pinned to the date it happened. The log is fed to Claude so advice can
  explain readings from real history and avoid redundant or unsafe re-dosing.
- **Analysis page with charts.** Per-parameter trend charts (server-rendered SVG,
  no JavaScript) with shaded target bands.
- **Full editable pool details.** Volume, sanitiser, surface, indoor/outdoor, plus
  **type, shape and dimensions** (length, width, average depth). A water-volume
  estimate is calculated from the dimensions as a cross-check.
- **Weather correlation.** Historical daily weather (temperature, rain, UV) for the
  pool's location is fetched from Open-Meteo, shown on a weather card next to each
  reading, and fed to Claude to explain weather-driven trends. The card opens a
  **5-day forecast** dialog (fetched server-side, only when you open it).
- **Local time.** Reading times are shown in the pool's own timezone, derived from
  its location.
- **Import / export / snapshot.**
  - Export or import the full reading history as standard JSON (import de-dupes).
  - One-click **4-hour snapshot**: a machine- and human-readable JSON of the last
    4 hours of readings plus full pool details and unit labels, handy for feeding
    into other tools or LLMs.
- **Delete** individual readings, with full history kept otherwise.
- **Runs in one Docker container.**

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env: set a strong APP_SECRET. Optionally set ANTHROPIC_API_KEY (for AI
# advice / strip reading) and an email provider (RESEND_API_KEY or SMTP_*).

docker compose up --build
```

Open http://localhost:8123, enter your email, and you're in.

### Without an email provider or API key

The app is fully usable out of the box:

- **No email provider?** It runs in *console mode*: the login link is printed to
  the container logs, shown on screen, and written to `/data/outbox/`.
- **No `ANTHROPIC_API_KEY`?** Advice falls back to a basic in-range / out-of-range
  check (no dosing), and strip-photo reading is disabled. Set a key for the full
  Claude features.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `APP_SECRET` | Signs sessions & magic-link tokens, and derives the key that encrypts stored device credentials. **Set this.** |
| `BASE_URL` | Public URL used to build login links (e.g. `https://pool.example.com`). |
| `ANTHROPIC_API_KEY` | Enables Claude advice and test-strip reading. Without it, a basic fallback is used. |
| `ADVICE_MODEL` / `ADVICE_EFFORT` | Claude model (default `claude-sonnet-5`) and thinking effort (`low`/`medium`/`high`/`max`). |
| `AUTO_SYNC_INTERVAL_HOURS` | How often the background scheduler syncs auto-enabled devices (default `1`; `0` disables it). |
| `RESEND_API_KEY` | Send magic-link emails via [Resend](https://resend.com). Takes priority over SMTP. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` | SMTP delivery, used when `RESEND_API_KEY` is empty. |
| `EMAIL_FROM` | From address for login emails. Must be on a domain you've verified with your provider. |
| `MAGIC_LINK_TTL_MINUTES` / `SESSION_TTL_DAYS` | Login link and session lifetimes. |
| `DATA_DIR` | Where the SQLite DB, uploaded photos and dev outbox live (default `/data`, a Docker volume). |

Email provider is chosen automatically: **Resend** if `RESEND_API_KEY` is set,
else **SMTP** if `SMTP_HOST` is set, else **console**.

## How it works

```
app/
  main.py            FastAPI app: sessions, static, startup, /healthz
  config.py          Env-based settings (data/uploads dirs, email provider)
  database.py        SQLAlchemy engine + lightweight column migration
  models.py          User, MagicToken, Pool, Reading, PoolAdvice,
                     PoolContextNote, ProviderCredential, WeatherDay
                     + pool volume estimator
  security.py        Token hashing + Fernet encryption for device credentials
  email_utils.py     Magic-link delivery (Resend / SMTP / console)
  auth.py            Magic-link issue/consume + session helpers
  chemistry.py       Shared types, target ranges, deterministic fallback
  advice.py          Claude-powered advice (structured output, prompt caching)
  sync_service.py    Pull + store device readings (manual route + scheduler)
  scheduler.py       Background loop that auto-syncs enabled devices
  vision.py          Claude vision: read a test strip from a photo
  charts.py          Dependency-free inline-SVG trend charts
  weather.py         Open-Meteo geocoding, cached daily weather, timezone lookup
  templating.py      Jinja env + local-time formatting helper
  integrations/      Device adapters behind a common interface
    base.py            PoolDevice interface + normalised DeviceMeasurement
    aiper.py           Aiper cloud (encrypted REST login + device list)
    aiper_shadow.py    Aiper water quality via AWS IoT MQTT shadow (SigV4 WS)
    blueriiot.py       Blueriiot Blue Connect adapter (AWS SigV4)
  routes/            Web routes (auth, pools/readings/advice/analysis,
                     snapshot, import/export, integrations)
  templates/, static/  Server-rendered UI, logo and stylesheet
```

### Advice

`advice.py` sends the pool spec (including dimensions and the calculated volume
estimate), recent reading history with each day's weather, your notes, and the
pool's dated context log to Claude using the Messages API with **structured
outputs** and **prompt caching** on the stable expert system prompt. The result is stored in `pool_advice` and
shown on the pool page; it is only regenerated when you add a reading or press
**Refresh**. If the API key is missing or a call fails, it falls back to
`chemistry.fallback_assessment` so the page always renders.

### Test-strip photos

`vision.py` sends an uploaded strip photo to Claude, which compares each pad to
the colour key in the same image and returns numeric values. These pre-fill the
reading form for you to confirm before saving. Anything it can't read confidently
is left blank rather than guessed. Uploads are validated (type and size) and
served back through an ownership-checked route.

### Analysis and charts

The analysis page renders one trend chart per parameter as inline SVG (no
JavaScript, no CDN), with a shaded target band derived from the published ranges
(the free-chlorine band adapts to stabiliser level and pool type). Times use the
pool's local timezone.

### Import, export and snapshot

- **Export / import** the full history as JSON. Import accepts the export envelope
  or a bare array and de-dupes on `(source, external_id, taken_at)`, so
  re-importing is idempotent. The export also carries the pool's dated **context
  log**, which import restores and de-dupes on `(date, note)`. Images are not
  included.
- **Snapshot (4h)** emits a JSON document describing the pool (type, shape,
  dimensions, volume and estimate, sanitiser, surface, location, timezone, notes
  and context log) plus every reading from the last 4 hours, with UTC and local
  timestamps and a `units` map. It is designed to be dropped into other tools or
  LLMs.

### Device integrations

Each adapter is isolated behind the `PoolDevice` interface and normalises
whatever the vendor returns into the app's canonical units (ppm, mV, °C).
Credentials you enter are **encrypted at rest** (Fernet, key derived from
`APP_SECRET`) and only used to fetch your own measurements.

For Aiper, login and device discovery use the vendor's encrypted REST API
(region-selectable: Europe, Americas, Asia), but the HydroComm reports water
quality only over MQTT, so `aiper_shadow.py` connects to AWS IoT over a
SigV4-signed WebSocket and reads the device shadow. Neither Aiper nor Blueriiot
publishes an official API; those endpoints are based on community
reverse-engineering and may need adjusting if a vendor changes their API.

**PoolLab (LabCOM)** covers Water-i.d.'s PoolLab 1.0/2.0 photometers, which sync
via the LabCOM app to the LabCOM cloud. Connect it with an API token generated
in your account settings at [labcom.cloud](https://labcom.cloud) (not your
password). The adapter queries LabCOM's GraphQL API
(`backend.labcom.cloud/graphql`), skips the demo "tutorial" measurements and
out-of-range results (`OVERRANGE`/`UNDERRANGE`), and groups single-parameter
photometer tests taken within an hour of each other into one reading per test
session.

### MQTT publishing

Set `MQTT_HOST` (plus optional `MQTT_PORT`, `MQTT_USERNAME`/`MQTT_PASSWORD`,
`MQTT_USE_TLS`, `MQTT_TOPIC_PREFIX`) and the background scheduler pushes
readings to your broker every `MQTT_PUBLISH_INTERVAL_MINUTES` (default 15),
for consumption by Home Assistant, Node-RED and the like:

- `pool_tracking/<pool_id>/readings` — each reading stored since the last
  publish, oldest first (QoS 1). Watermarked by row id, so history backfilled
  by a device sync or import is published too.
- `pool_tracking/<pool_id>/latest` — the pool's most recent reading (QoS 1,
  **retained**), so a new subscriber immediately gets current state.

Payloads are JSON with the pool id/name, reading id, `taken_at` (UTC ISO-8601),
`source`, and the chemistry fields in the app's canonical units. Because
different sources measure different subsets, every message carries the pool's
**last-known value for each measurement** (as of that reading's time), each
paired with a `<field>_measured_at` timestamp so consumers can tell how fresh
it is; fields the pool has never measured are omitted rather than null:

```json
{
  "pool_id": 1, "pool_name": "Garden Pool", "reading_id": 42,
  "taken_at": "2026-07-06T10:00:00+00:00", "source": "poollab", "external_id": "…",
  "ph": 7.3, "ph_measured_at": "2026-07-06T10:00:00+00:00",
  "total_alkalinity": 105.0, "total_alkalinity_measured_at": "2026-07-01T09:15:00+00:00"
}
```

Publish failures are retried on the next interval; nothing is dropped while
the broker is unreachable (except across an app restart).

#### Using with Home Assistant

Point the app at the same broker Home Assistant uses — with Home Assistant OS
(hass.io) that's usually the **Mosquitto broker** add-on:

1. In Home Assistant: *Settings → Add-ons → Mosquitto broker* (install and
   start it), then *Settings → Devices & Services* and configure the **MQTT**
   integration to use it. Create a Home Assistant user for this app to log in
   with (the add-on accepts any HA user).
2. In this app's `.env`, point at the broker and restart:

   ```bash
   MQTT_HOST=homeassistant.local   # or the HA IP
   MQTT_PORT=1883
   MQTT_USERNAME=pool-tracking     # the HA user you created
   MQTT_PASSWORD=...
   ```

3. Define sensors that read from the retained `latest` topic. Your pool's id
   is in its page URL (`/pools/1` → topic `pool_tracking/1/latest`). In
   `configuration.yaml`:

   ```yaml
   mqtt:
     sensor:
       - name: "Pool pH"
         unique_id: pool_1_ph
         state_topic: "pool_tracking/1/latest"
         value_template: "{{ value_json.ph }}"
         device_class: ph
         state_class: measurement

       - name: "Pool free chlorine"
         unique_id: pool_1_free_chlorine
         state_topic: "pool_tracking/1/latest"
         value_template: "{{ value_json.free_chlorine }}"
         unit_of_measurement: "ppm"
         state_class: measurement

       - name: "Pool temperature"
         unique_id: pool_1_temperature
         state_topic: "pool_tracking/1/latest"
         value_template: "{{ value_json.temperature_c }}"
         device_class: temperature
         unit_of_measurement: "°C"
         state_class: measurement

       - name: "Pool last reading"
         unique_id: pool_1_last_reading
         state_topic: "pool_tracking/1/latest"
         value_template: "{{ value_json.taken_at }}"
         device_class: timestamp

       # Each measurement also carries its own timestamp, so you can track
       # when a value was actually tested (useful for occasional photometer
       # parameters like CYA that persist across readings).
       - name: "Pool pH measured at"
         unique_id: pool_1_ph_measured_at
         state_topic: "pool_tracking/1/latest"
         value_template: "{{ value_json.ph_measured_at }}"
         device_class: timestamp
   ```

   Add more of the same for `total_alkalinity`, `cyanuric_acid`,
   `calcium_hardness`, `salt` (all ppm), `orp` (mV), `ec` (µS/cm) or `tds`
   (ppm) as your devices report them. Reload YAML (*Developer tools → YAML →
   All YAML configuration*) or restart Home Assistant.

Because `latest` is retained, the sensors populate as soon as Home Assistant
subscribes — no need to wait for the next reading. Values persist across
readings that don't re-measure them (the message always carries the last-known
value per field, with its `<field>_measured_at`), so sensors don't flap to
`unknown` between test sessions. A field the pool has *never* measured is
absent from the payload and its sensor stays `unknown` — only define sensors
for fields your sources report. Automations can also trigger on every stored
reading via the `pool_tracking/1/readings` stream topic.

### Weather and timezone

When a pool has a location, the daily weather for each reading date is fetched from
[Open-Meteo](https://open-meteo.com) (no API key) and cached in the `weather_days`
table. The pool's IANA timezone is also looked up from its coordinates and stored,
so reading times display in local time.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://localhost:8000

pytest                              # run the test suite
```

The schema evolves through a lightweight migration in `database.py`
(`_add_missing_columns`), which adds any missing columns on startup. There is no
separate migration tool.

## Privacy

The app is self-hosted, so data lives in your database and data volume. A few
features send some of it to third parties (there's an in-app summary at
`/privacy`):

- **Anthropic (Claude)** receives pool details, readings, notes and the context
  log for advice, and **the uploaded photo** when reading a test strip. Only when
  `ANTHROPIC_API_KEY` is set.
- **Open-Meteo** receives a pool's coordinates (and place name when geocoding)
  for weather and timezone. These coordinates identify where your pool is. Leave
  a pool's location blank to avoid this.
- **Your email provider** (Resend or SMTP) receives your email address and login
  link.
- **Device clouds** (Aiper/Blueriiot, and AWS IoT for Aiper) are accessed with
  your own account credentials, which are encrypted at rest.

Photos and pool details (including location) are stored unencrypted in the data
volume. JSON exports and the 4-hour snapshot include location, notes and the
context log (but not images), so be mindful when pasting them into other tools or
LLMs.

## Notes & disclaimer

Dosing advice is an estimate to guide a non-expert owner: always add chemicals
gradually with the pump running, never mix chemicals, and re-test before adding
more. This app is not a substitute for professional advice on a pool you're unsure
about.
