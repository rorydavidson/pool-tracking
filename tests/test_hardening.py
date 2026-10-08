"""Deployment safety checks: dev-mode login links, secrets, cookies, logout."""
from __future__ import annotations

from app.config import Settings, get_settings


def test_console_mode_without_dev_mode_refuses_login(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dev_mode", False)
    resp = client.post("/auth/request", data={"email": "someone@example.com"})
    assert resp.status_code == 503
    assert "/auth/verify?token=" not in resp.text


def test_dev_mode_requires_localhost_base_url():
    remote = Settings(dev_mode=True, base_url="https://pool.example.com")
    assert any("DEV_MODE" in p for p in remote.startup_problems())
    local = Settings(dev_mode=True, base_url="http://127.0.0.1:8000")
    assert not any("DEV_MODE" in p for p in local.startup_problems())


_STRONG = "x" * 40


def test_default_or_short_secret_refused_outside_dev_mode():
    for secret in ("dev-insecure-secret-change-me", "change-me-to-a-long-random-string", "short"):
        s = Settings(app_secret=secret, base_url="https://pool.example.com", dev_mode=False)
        problems = s.startup_problems()
        assert any("APP_SECRET" in p for p in problems), secret


def test_strong_secret_accepted():
    s = Settings(app_secret=_STRONG, base_url="https://pool.example.com", dev_mode=False)
    assert s.startup_problems() == []


def test_weak_secret_tolerated_in_dev_mode():
    s = Settings(app_secret="dev-insecure-secret-change-me", dev_mode=True)
    assert s.startup_problems() == []
