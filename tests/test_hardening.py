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
