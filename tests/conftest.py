"""Test configuration: isolate data dir and secrets before the app imports."""
from __future__ import annotations

import os
import tempfile

# Must be set before any app module imports get_settings() / builds the engine.
_TMP = tempfile.mkdtemp(prefix="pool-tracking-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["APP_SECRET"] = "test-secret-not-for-production"
os.environ["BASE_URL"] = "http://testserver"
os.environ.setdefault("SMTP_HOST", "")  # console email mode
os.environ.setdefault("ANTHROPIC_API_KEY", "")  # fallback advice (no network)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def logged_in_client(client):
    """A client with an authenticated session via the magic-link flow."""
    resp = client.post("/auth/request", data={"email": "tester@example.com"})
    assert resp.status_code == 200
    # In console mode the verify link is surfaced in the page body.
    import re

    match = re.search(r'/auth/verify\?token=([^"&\s]+)', resp.text)
    assert match, "magic link not found in login_sent page"
    verify = client.get(f"/auth/verify?token={match.group(1)}", follow_redirects=False)
    assert verify.status_code == 303
    return client
