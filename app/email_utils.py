"""Magic-link email delivery.

When SMTP is configured the link is emailed. Otherwise the app runs in
"console" mode: the link is logged and written to ``$DATA_DIR/outbox`` so the
app is fully usable for local development and demos with no mail server.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .config import Settings, get_settings

logger = logging.getLogger("pool_tracking.email")


def _build_message(settings: Settings, to_email: str, link: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Your Pool Tracking login link"
    msg["From"] = settings.email_from
    msg["To"] = to_email
    ttl = settings.magic_link_ttl_minutes
    msg.set_content(
        "Hi,\n\n"
        "Use the link below to sign in to Pool Tracking. "
        f"It expires in {ttl} minutes and can only be used once.\n\n"
        f"{link}\n\n"
        "If you didn't request this, you can safely ignore this email.\n"
    )
    msg.add_alternative(
        f"""\
<html><body style="font-family:system-ui,Arial,sans-serif">
  <h2>Sign in to Pool Tracking</h2>
  <p>Click the button below to sign in. This link expires in {ttl} minutes
     and can only be used once.</p>
  <p><a href="{link}"
        style="display:inline-block;padding:12px 20px;background:#0b7285;
               color:#fff;border-radius:6px;text-decoration:none">
     Sign in</a></p>
  <p style="color:#666;font-size:13px">If you didn't request this, ignore this email.</p>
</body></html>
""",
        subtype="html",
    )
    return msg


def send_magic_link(to_email: str, link: str) -> None:
    settings = get_settings()
    msg = _build_message(settings, to_email, link)

    if not settings.email_enabled:
        _deliver_to_console(settings, to_email, link, msg)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
    logger.info("Sent magic link to %s", to_email)


def _deliver_to_console(settings: Settings, to_email: str, link: str, msg: EmailMessage) -> None:
    settings.ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = to_email.replace("@", "_at_").replace("/", "_")
    out: Path = settings.outbox_dir / f"{stamp}_{safe}.eml"
    out.write_text(msg.as_string(), encoding="utf-8")
    logger.warning(
        "SMTP not configured — magic link for %s NOT emailed.\n"
        "  Link: %s\n  Saved to: %s",
        to_email,
        link,
        out,
    )
