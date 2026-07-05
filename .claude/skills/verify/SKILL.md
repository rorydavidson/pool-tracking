---
name: verify
description: Build, run, and drive this app to verify a change end-to-end (FastAPI + Jinja, SQLite, magic-link auth).
---

# Verifying pool-tracking changes

## Launch

```bash
pip install -r requirements.txt
DATA_DIR=$(mktemp -d) APP_SECRET=verify-secret BASE_URL=http://127.0.0.1:8000 \
  SMTP_HOST= ANTHROPIC_API_KEY= \
  uvicorn app.main:app --port 8000
```

- `SMTP_HOST=` (empty) puts email in console mode: the magic login link is
  printed **in the login_sent page body**, so curl can log in without a mailbox.
- `ANTHROPIC_API_KEY=` (empty) uses the fallback (non-Claude) advice path.

## Log in with curl

```bash
LINK=$(curl -s -c /tmp/j -X POST http://127.0.0.1:8000/auth/request \
  -d "email=verify@example.com" | grep -o '/auth/verify?token=[^"]*' | head -1)
curl -s -b /tmp/j -c /tmp/j "http://127.0.0.1:8000$LINK"   # 303 → logged in
```

## Flows worth driving

- Create pool: `POST /pools/new` (`name`, `volume`, `volume_unit=litres`) → 303 to `/pools/{id}`.
- Manual reading: `POST /pools/{id}/readings/new`.
- Integrations: `/integrations` page; `POST /integrations/{provider}/connect`,
  `/sync` (form `pool_id`), `/autosync`, `/disconnect`. Outcomes surface as
  `?flash=` / `?error=` on the redirect URL — assert on `%{redirect_url}`.
- Full readings history renders on `/pools/{id}/analysis` (the pool page's
  history panel shows **today only**); JSON at `/pools/{id}/export`.

## Gotchas

- Device adapters call vendor clouds directly (module-level `httpx`). To verify
  a sync end-to-end, run a local HTTP stub and point the adapter's module-level
  URL constant at it from a small launcher script that then calls
  `uvicorn.run(app)` (e.g. `app.integrations.poollab.GRAPHQL_URL = "http://127.0.0.1:8765/graphql"`).
- The sandbox proxy blocks most vendor endpoints anyway; don't burn time trying
  to reach them live.
- Old-dated readings don't appear on the pool page's "Today" panel — check
  `/analysis` or `/export` instead.
