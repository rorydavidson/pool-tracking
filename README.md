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
| `ADVICE_MODEL` / `ADVICE_EFFORT` | Claude model (default `claude-opus-4-8`) and thinking effort (`low`/`medium`/`high`/`max`). |
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
                     ProviderCredential, WeatherDay + pool volume estimator
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
estimate), recent reading history with each day's weather, and your notes to
Claude using the Messages API with **structured outputs** and **prompt caching**
on the stable expert system prompt. The result is stored in `pool_advice` and
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
  re-importing is idempotent. Images are not included.
- **Snapshot (4h)** emits a JSON document describing the pool (type, shape,
  dimensions, volume and estimate, sanitiser, surface, location, timezone) plus
  every reading from the last 4 hours, with UTC and local timestamps and a `units`
  map. It is designed to be dropped into other tools or LLMs.

### Device integrations

Neither Aiper nor Blueriiot publishes an official API, so each adapter is isolated
behind the `PoolDevice` interface and normalises whatever the vendor returns into
the app's canonical units (ppm, mV, °C). Credentials you enter are **encrypted at
rest** (Fernet, key derived from `APP_SECRET`) and only used to fetch your own
measurements.

For Aiper, login and device discovery use the vendor's encrypted REST API
(region-selectable: Europe, Americas, Asia), but the HydroComm reports water
quality only over MQTT, so `aiper_shadow.py` connects to AWS IoT over a
SigV4-signed WebSocket and reads the device shadow. Endpoints are based on
community reverse-engineering and may need adjusting if a vendor changes their API.

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

- **Anthropic (Claude)** receives pool details, readings and notes for advice,
  and **the uploaded photo** when reading a test strip. Only when
  `ANTHROPIC_API_KEY` is set.
- **Open-Meteo** receives a pool's coordinates (and place name when geocoding)
  for weather and timezone. These coordinates identify where your pool is. Leave
  a pool's location blank to avoid this.
- **Your email provider** (Resend or SMTP) receives your email address and login
  link.
- **Device clouds** (Aiper/Blueriiot, and AWS IoT for Aiper) are accessed with
  your own account credentials, which are encrypted at rest.

Photos and pool details (including location) are stored unencrypted in the data
volume. JSON exports and the 4-hour snapshot include location and notes (but not
images), so be mindful when pasting them into other tools or LLMs.

## Notes & disclaimer

Dosing advice is an estimate to guide a non-expert owner: always add chemicals
gradually with the pump running, never mix chemicals, and re-test before adding
more. This app is not a substitute for professional advice on a pool you're unsure
about.
