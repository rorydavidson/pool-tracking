# Pool Tracking

A self-hostable web app that tracks the chemical balance of home swimming pools,
gives **AI-generated advice** on how to correct the water, and links your
readings to the **real weather** on the day they were taken so you can spot what
is driving changes in your pool.

- **Passwordless login** — sign in with a one-time email link (magic link). Each
  user gets their own pools, readings and device connections.
- **Manual or automatic readings** — type in a test kit result, or pull readings
  straight from your devices:
  - **Aiper HydroComm** smart pool monitor (pH, ORP, EC/TDS, free chlorine, temp)
  - **Blueriiot Blue Connect** probe (pH, ORP, temperature, salinity)
- **On-the-fly advice from Claude** — every reading is assessed by Claude
  (Opus 4.8), which reasons over the whole picture (pool volume, sanitiser type,
  stabiliser, recent history *and* the weather) and returns prioritised,
  **dosed** recommendations tailored to your pool.
- **Full history, kept forever** — every reading is stored and shown in a
  history table; nothing is pruned.
- **Weather correlation** — historical daily weather (temperature, rain, UV) for
  the pool's location is fetched from Open-Meteo and shown next to each reading,
  and fed to Claude so it can explain weather-driven trends (e.g. hot, high-UV
  days burning off chlorine; heavy rain dropping pH).
- **Runs in one Docker container.**

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env: set a strong APP_SECRET. Optionally set ANTHROPIC_API_KEY (for AI
# advice) and SMTP_* (to actually email login links).

docker compose up --build
```

Open http://localhost:8000, enter your email, and you're in.

### Without SMTP or an API key

The app is fully usable out of the box:

- **No `SMTP_HOST`?** It runs in *console mode* — the login link is printed to
  the container logs and shown on screen, and written to `/data/outbox/`.
- **No `ANTHROPIC_API_KEY`?** Advice falls back to a basic in-range / out-of-range
  check (no dosing). Set a key to get the full Claude-generated advice.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `APP_SECRET` | Signs sessions & magic-link tokens, and derives the key that encrypts stored device credentials. **Set this.** |
| `BASE_URL` | Public URL used to build login links (e.g. `https://pool.example.com`). |
| `ANTHROPIC_API_KEY` | Enables Claude-generated advice. Without it, a basic fallback is used. |
| `ADVICE_MODEL` / `ADVICE_EFFORT` | Claude model (default `claude-opus-4-8`) and thinking effort (`low`/`medium`/`high`/`max`). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `EMAIL_FROM` | Outbound email for magic links. Leave `SMTP_HOST` blank for console mode. |
| `MAGIC_LINK_TTL_MINUTES` / `SESSION_TTL_DAYS` | Login link and session lifetimes. |
| `DATA_DIR` | Where the SQLite DB and dev outbox live (default `/data`, a Docker volume). |

## How it works

```
app/
  main.py            FastAPI app: sessions, static, startup, /healthz
  config.py          Env-based settings
  database.py        SQLAlchemy engine + lightweight column migration
  models.py          User, MagicToken, Pool, Reading, ProviderCredential, WeatherDay
  security.py        Token hashing + Fernet encryption for device credentials
  email_utils.py     Magic-link delivery (SMTP or console)
  auth.py            Magic-link issue/consume + session helpers
  chemistry.py       Shared types, target ranges, deterministic fallback
  advice.py          Claude-powered advice (structured output, prompt caching)
  weather.py         Open-Meteo geocoding + cached historical daily weather
  integrations/      Device adapters behind a common interface
    base.py            PoolDevice interface + normalised DeviceMeasurement
    aiper.py           Aiper HydroComm cloud REST adapter
    blueriiot.py       Blueriiot Blue Connect adapter (AWS SigV4)
  routes/            Web routes (auth, pools/readings/advice, integrations)
  templates/, static/  Server-rendered UI
```

### Advice

`advice.py` sends the pool spec + recent reading history (with each day's
weather) to Claude using the Messages API with **structured outputs** (so the
result is a typed list of recommendations) and **prompt caching** on the stable
expert system prompt. If the API key is missing or a call fails, it falls back
to `chemistry.fallback_assessment` so the page always renders.

### Device integrations

Neither Aiper nor Blueriiot publishes an official API, so each adapter is
isolated behind the `PoolDevice` interface and normalises whatever the vendor
returns into the app's canonical units (ppm, mV, °C). Credentials you enter are
**encrypted at rest** (Fernet, key derived from `APP_SECRET`) and only used to
fetch your own measurements. Endpoints are based on community
reverse-engineering and may need adjusting if a vendor changes their API.

### Weather

When a pool has a location, the daily weather for each reading date is fetched
from [Open-Meteo](https://open-meteo.com) (no API key) and cached in the
`weather_days` table. It's shown in the UI and passed to Claude to correlate
chemistry changes with conditions.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://localhost:8000

pytest                              # run the test suite
```

## Notes & disclaimer

Dosing advice is an estimate to guide a non-expert owner — always add chemicals
gradually with the pump running, never mix chemicals, and re-test before adding
more. This app is not a substitute for professional advice on a pool you're
unsure about.
