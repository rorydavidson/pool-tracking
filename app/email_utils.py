"""Magic-link email delivery.

Delivery provider is picked automatically (see ``Settings.email_provider``):

* **resend**  — sent via the Resend HTTP API (https://resend.com).
* **smtp**    — sent via a configured SMTP server.
* **console** — no provider configured: the link is logged and written to
  ``$DATA_DIR/outbox`` so the app stays usable for local dev and demos.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx

from .config import Settings, get_settings

logger = logging.getLogger("pool_tracking.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _email_content(settings: Settings, link: str) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for the magic-link email."""
    ttl = settings.magic_link_ttl_minutes
    subject = "Your Pool Tracking login link"
    text = (
        "Hi,\n\n"
        "Use the link below to sign in to Pool Tracking. "
        f"It expires in {ttl} minutes and can only be used once.\n\n"
        f"{link}\n\n"
        "If you didn't request this, you can safely ignore this email.\n"
    )
    html = (
        '<html><body style="font-family:system-ui,Arial,sans-serif">'
        "<h2>Sign in to Pool Tracking</h2>"
        f"<p>Click the button below to sign in. This link expires in {ttl} minutes "
        "and can only be used once.</p>"
        f'<p><a href="{link}" '
        'style="display:inline-block;padding:12px 20px;background:#0b7285;'
        'color:#fff;border-radius:6px;text-decoration:none">Sign in</a></p>'
        '<p style="color:#666;font-size:13px">If you didn\'t request this, '
        "ignore this email.</p>"
        "</body></html>"
    )
    return subject, text, html


def _build_message(settings: Settings, to_email: str, link: str) -> EmailMessage:
    subject, text, html = _email_content(settings, link)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_magic_link(to_email: str, link: str) -> None:
    settings = get_settings()
    provider = settings.email_provider

    if provider == "resend":
        _send_via_resend(settings, to_email, link)
    elif provider == "smtp":
        _send_via_smtp(settings, to_email, link)
    else:
        _deliver_to_console(settings, to_email, link)


def _send_via_resend(settings: Settings, to_email: str, link: str) -> None:
    subject, text, html = _email_content(settings, link)
    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from,
                "to": [to_email],
                "subject": subject,
                "html": html,
                "text": text,
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Resend: {exc}") from exc
    if resp.status_code >= 400:
        # Resend returns a JSON error body; surface its message if present.
        detail = resp.text
        try:
            detail = resp.json().get("message", detail)
        except ValueError:
            pass
        raise RuntimeError(f"Resend rejected the email ({resp.status_code}): {detail}")
    logger.info("Sent magic link to %s via Resend", to_email)


def _send_via_smtp(settings: Settings, to_email: str, link: str) -> None:
    msg = _build_message(settings, to_email, link)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
    logger.info("Sent magic link to %s via SMTP", to_email)


def _deliver_to_console(settings: Settings, to_email: str, link: str) -> None:
    settings.ensure_dirs()
    msg = _build_message(settings, to_email, link)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = to_email.replace("@", "_at_").replace("/", "_")
    out: Path = settings.outbox_dir / f"{stamp}_{safe}.eml"
    out.write_text(msg.as_string(), encoding="utf-8")
    logger.warning(
        "No email provider configured — magic link for %s NOT emailed.\n"
        "  Link: %s\n  Saved to: %s",
        to_email,
        link,
        out,
    )
