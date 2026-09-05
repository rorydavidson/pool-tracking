"""Passwordless (magic-link) email authentication."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .email_utils import send_magic_link
from .models import MagicToken, User
from .security import generate_token, hash_token

SESSION_USER_KEY = "user_id"
logger = logging.getLogger("pool_tracking.auth")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_user(db: Session, email: str) -> User:
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def issue_magic_link(db: Session, email: str) -> str | None:
    """Create a single-use login token for ``email`` and email the link.

    Returns the absolute login URL (also handy for tests / console mode), or
    None when the address is not on the ALLOWED_EMAILS list. In that case no
    user row is created and nothing is sent.
    """
    settings = get_settings()
    if not settings.is_email_allowed(email):
        logger.info("Login requested for address not in ALLOWED_EMAILS; ignoring")
        return None
    user = get_or_create_user(db, email)

    token = generate_token()
    db.add(
        MagicToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=_utcnow() + timedelta(minutes=settings.magic_link_ttl_minutes),
        )
    )
    db.commit()

    link = f"{settings.base_url.rstrip('/')}/auth/verify?token={token}"
    send_magic_link(user.email, link)
    return link


def consume_magic_link(db: Session, token: str) -> User | None:
    """Validate and burn a magic token. Returns the user, or None if invalid."""
    record = db.scalar(select(MagicToken).where(MagicToken.token_hash == hash_token(token)))
    if record is None or record.used_at is not None:
        return None
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < _utcnow():
        return None

    record.used_at = _utcnow()
    user = db.get(User, record.user_id)
    if user is not None:
        user.last_login_at = _utcnow()
    db.commit()
    return user


def login_session(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id


def logout_session(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Dependency: the logged-in user, or None."""
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = db.get(User, user_id)
    # Accounts created before the allowlist was set (or later removed from it)
    # lose access immediately rather than when their session cookie expires.
    if user is not None and not get_settings().is_email_allowed(user.email):
        request.session.pop(SESSION_USER_KEY, None)
        return None
    return user
