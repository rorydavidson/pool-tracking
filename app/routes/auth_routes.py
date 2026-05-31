"""Login / logout routes (magic-link email auth)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..database import get_db
from ..templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user=Depends(auth.current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, user=Depends(auth.current_user)):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "user": user,
            "claude_enabled": bool(settings.anthropic_api_key),
            "email_provider": settings.email_provider,
        },
    )


@router.post("/auth/request", response_class=HTMLResponse)
def request_link(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        link = auth.issue_magic_link(db, email)
    except RuntimeError:
        # Email provider (e.g. Resend) failed; don't 500 — show a friendly error.
        logging.getLogger("pool_tracking.auth").exception("Failed to send magic link")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "We couldn't send the login email just now. Please try again shortly."},
            status_code=502,
        )
    # In console mode (no provider), surface the link so the app is usable without mail.
    dev_link = None if settings.email_enabled else link
    return templates.TemplateResponse(
        request,
        "login_sent.html",
        {"email": email.strip().lower(), "dev_link": dev_link},
    )


@router.get("/auth/verify", response_class=HTMLResponse)
def verify(request: Request, token: str, db: Session = Depends(get_db)):
    user = auth.consume_magic_link(db, token)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That login link is invalid or has expired."},
            status_code=400,
        )
    auth.login_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)
