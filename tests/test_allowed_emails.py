"""ALLOWED_EMAILS: only listed addresses can sign in or keep a session."""
from __future__ import annotations

import re

import pytest

from app.config import get_settings


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setattr(get_settings(), "allowed_emails", "Owner@Example.com, second@example.com")


def _login(client, email):
    resp = client.post("/auth/request", data={"email": email})
    assert resp.status_code == 200
    match = re.search(r'/auth/verify\?token=([^"&\s]+)', resp.text)
    return match.group(1) if match else None


def test_parsing_normalises_case_and_whitespace(allowlist):
    s = get_settings()
    assert s.allowed_email_set == {"owner@example.com", "second@example.com"}
    assert s.is_email_allowed("OWNER@example.com ")
    assert not s.is_email_allowed("intruder@example.com")


def test_empty_allowlist_permits_everyone():
    assert get_settings().allowed_emails == ""
    assert get_settings().is_email_allowed("anyone@example.com")


def test_unlisted_email_gets_no_link_and_no_account(client, allowlist):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User

    resp = client.post("/auth/request", data={"email": "intruder@example.com"})
    # Same page as a real request, so the form can't be used to probe the list.
    assert resp.status_code == 200
    assert "intruder@example.com" in resp.text
    assert "/auth/verify?token=" not in resp.text
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.email == "intruder@example.com")) is None


def test_listed_email_can_log_in(client, allowlist):
    token = _login(client, "owner@example.com")
    assert token
    assert client.get(f"/auth/verify?token={token}", follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200


def test_existing_session_revoked_when_removed_from_list(client, monkeypatch):
    token = _login(client, "legacy@example.com")
    assert token
    client.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert client.get("/").status_code == 200

    monkeypatch.setattr(get_settings(), "allowed_emails", "owner@example.com")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
