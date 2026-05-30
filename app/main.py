"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import get_settings
from .database import init_db
from .routes import auth_routes, integrations, pools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = get_settings()

app = FastAPI(title="Pool Tracking", version=__version__)

# Signed-cookie sessions hold only the logged-in user id.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    max_age=settings.session_ttl_days * 24 * 3600,
    same_site="lax",
    https_only=False,  # set True behind HTTPS in production
)

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(auth_routes.router)
app.include_router(pools.router)
app.include_router(integrations.router)


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    init_db()
    logging.getLogger("pool_tracking").info(
        "Started Pool Tracking %s (email=%s, advice=%s)",
        __version__,
        "smtp" if settings.email_enabled else "console",
        "claude" if settings.anthropic_api_key else "fallback",
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})
