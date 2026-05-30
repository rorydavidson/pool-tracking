"""End-to-end-ish tests for auth, pools, readings, and integrations."""
from __future__ import annotations


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dashboard_requires_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_magic_link_login_flow(client):
    resp = client.post("/auth/request", data={"email": "alice@example.com"})
    assert resp.status_code == 200
    assert "alice@example.com" in resp.text
    # Console mode surfaces the link.
    import re

    match = re.search(r'/auth/verify\?token=([^"&\s]+)', resp.text)
    assert match
    verify = client.get(f"/auth/verify?token={match.group(1)}", follow_redirects=False)
    assert verify.status_code == 303
    # Now the dashboard is reachable.
    dash = client.get("/")
    assert dash.status_code == 200
    assert "Your pools" in dash.text


def test_invalid_token_rejected(client):
    resp = client.get("/auth/verify?token=nope", follow_redirects=False)
    assert resp.status_code == 400


def test_create_pool_and_reading_shows_advice(logged_in_client):
    client = logged_in_client
    # Create a pool.
    resp = client.post(
        "/pools/new",
        data={
            "name": "Garden Pool",
            "volume": "50000",
            "volume_unit": "litres",
            "sanitizer": "chlorine",
            "surface": "plaster",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pool_url = resp.headers["location"]
    assert pool_url.startswith("/pools/")

    # Add a reading with low chlorine.
    add = client.post(
        f"{pool_url}/readings/new",
        data={"ph": "7.4", "free_chlorine": "0.1", "total_alkalinity": "100"},
        follow_redirects=False,
    )
    assert add.status_code == 303

    # Pool page should render the assessment (fallback path, no API key in tests).
    page = client.get(pool_url)
    assert page.status_code == 200
    assert "Advice" in page.text
    assert "Free chlorine" in page.text


def test_cannot_view_another_users_pool(logged_in_client):
    from fastapi.testclient import TestClient

    from app.main import app

    # logged_in_client (tester@) creates a pool.
    resp = logged_in_client.post(
        "/pools/new",
        data={"name": "Private", "volume": "10000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = resp.headers["location"]

    # A fresh, unauthenticated client is redirected to login.
    with TestClient(app) as anon:
        other = anon.get(pool_url, follow_redirects=False)
    assert other.status_code == 303
    assert other.headers["location"] == "/login"


def test_integrations_page_lists_providers(logged_in_client):
    page = logged_in_client.get("/integrations")
    assert page.status_code == 200
    assert "Aiper HydroComm" in page.text
    assert "Blueriiot Blue Connect" in page.text
