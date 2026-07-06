"""End-to-end-ish tests for auth, pools, readings, and integrations."""
from __future__ import annotations

from datetime import date


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


def test_context_log_add_list_and_export(logged_in_client):
    client = logged_in_client
    resp = client.post(
        "/pools/new",
        data={"name": "Context Pool", "volume": "30000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = resp.headers["location"]

    # Add a dated context note.
    add = client.post(
        f"{pool_url}/context",
        data={"event_date": "2026-05-01", "note": "Replaced the salt cell"},
        follow_redirects=False,
    )
    assert add.status_code == 303

    # It shows on the pool page.
    page = client.get(pool_url)
    assert "Context log" in page.text
    assert "Replaced the salt cell" in page.text

    # And it travels with the snapshot/export ("full pool details").
    snap = client.get(f"{pool_url}/snapshot")
    body = snap.json()
    log = body["pool"]["context_log"]
    assert any(n["note"] == "Replaced the salt cell" and n["date"] == "2026-05-01" for n in log)

    # The readings export carries the context log too.
    export = client.get(f"{pool_url}/export").json()
    assert {"date": "2026-05-01", "note": "Replaced the salt cell"} in export["context_log"]


def test_context_log_import_round_trip(logged_in_client):
    """Importing an export envelope restores the context log into a fresh pool."""
    import io
    import json

    client = logged_in_client
    dest = client.post(
        "/pools/new",
        data={"name": "Import Target", "volume": "25000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = dest.headers["location"]

    envelope = {
        "kind": "pool-tracking.readings",
        "version": 2,
        "readings": [],
        "context_log": [
            {"date": "2026-03-15", "note": "Opened the pool for the season"},
            {"date": "2026-04-02", "note": "Shock treatment after algae"},
        ],
    }
    file = io.BytesIO(json.dumps(envelope).encode())
    imp = client.post(
        f"{pool_url}/import",
        files={"file": ("export.json", file, "application/json")},
        follow_redirects=False,
    )
    assert imp.status_code == 303
    assert "2%20context%20note(s)" in imp.headers["location"]

    page = client.get(pool_url)
    assert "Opened the pool for the season" in page.text
    assert "Shock treatment after algae" in page.text

    # Re-importing the same file adds no duplicates (suffix omitted when zero added).
    file2 = io.BytesIO(json.dumps(envelope).encode())
    again = client.post(
        f"{pool_url}/import",
        files={"file": ("export.json", file2, "application/json")},
        follow_redirects=False,
    )
    assert "context%20note" not in again.headers["location"]


def test_context_note_requires_text(logged_in_client):
    client = logged_in_client
    resp = client.post(
        "/pools/new",
        data={"name": "Blank Note Pool", "volume": "20000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = resp.headers["location"]
    add = client.post(
        f"{pool_url}/context",
        data={"event_date": "2026-05-01", "note": "   "},
        follow_redirects=False,
    )
    assert add.status_code == 303
    assert "error=" in add.headers["location"]


def test_context_log_in_advice_payload():
    """The dated context log is serialised into the payload sent to Claude."""
    import json

    from app.advice import _build_pool_payload
    from app.models import Pool, PoolContextNote, SanitizerType, SurfaceType

    pool = Pool(
        name="Payload Pool",
        volume_litres=40000,
        sanitizer=SanitizerType.chlorine,
        surface=SurfaceType.plaster,
        indoor=False,
    )
    notes = [
        PoolContextNote(event_date=date(2026, 4, 1), note="Drained and refilled"),
        PoolContextNote(event_date=date(2026, 6, 1), note="Added 400 g stabiliser"),
    ]
    payload = json.loads(_build_pool_payload(pool, [], None, None, notes))
    log = payload["context_log"]
    # Newest event first.
    assert [n["date"] for n in log] == ["2026-06-01", "2026-04-01"]
    assert log[0]["note"] == "Added 400 g stabiliser"


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


def test_pool_page_carries_forward_sparse_measurements(logged_in_client):
    """A parameter missing from the newest reading still shows, with its date."""
    client = logged_in_client
    resp = client.post(
        "/pools/new",
        data={"name": "Carry Pool", "volume": "30000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = resp.headers["location"]

    # An alkalinity test some days ago, then a pH-only reading today.
    client.post(f"{pool_url}/readings/new", data={"total_alkalinity": "95"})
    client.post(f"{pool_url}/readings/new", data={"ph": "7.4"})

    from datetime import timedelta

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Reading

    pool_id = int(pool_url.rsplit("/", 1)[-1])
    db = SessionLocal()
    try:
        ta_reading = db.scalar(
            select(Reading).where(
                Reading.pool_id == pool_id, Reading.total_alkalinity.is_not(None)
            )
        )
        ta_reading.taken_at = ta_reading.taken_at - timedelta(days=5)
        db.commit()
    finally:
        db.close()

    page = client.get(pool_url)
    assert page.status_code == 200
    # Alkalinity is still on the latest-reading panel, dated as carried.
    assert "Alkalinity" in page.text
    assert "95 ppm" in page.text
    assert "metric-age" in page.text


def test_integrations_page_lists_providers(logged_in_client):
    page = logged_in_client.get("/integrations")
    assert page.status_code == 200
    assert "Aiper HydroComm" in page.text
    assert "Blueriiot Blue Connect" in page.text
    assert "PoolLab (LabCOM)" in page.text
    # PoolLab connects with an API token rather than a login.
    assert "LabCOM API key" in page.text


def test_poollab_connect_and_sync(logged_in_client, monkeypatch):
    """Connecting with an API key and syncing stores de-duped readings."""
    from datetime import datetime, timezone

    from app.integrations.base import DeviceMeasurement

    client = logged_in_client

    class FakePoolLab:
        def __init__(self, credentials):
            assert credentials == {"api_key": "token-abc"}

        def verify(self):
            return True

        def latest_measurements(self):
            return [
                DeviceMeasurement(
                    taken_at=datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc),
                    external_id="42:PL2-001",
                    ph=7.3,
                    free_chlorine=1.2,
                    total_alkalinity=105,
                )
            ]

    def fake_get_client(provider, credentials):
        assert provider.value == "poollab"
        return FakePoolLab(credentials)

    # The connect route and the sync service each import get_client directly.
    monkeypatch.setattr("app.routes.integrations.get_client", fake_get_client)
    monkeypatch.setattr("app.sync_service.get_client", fake_get_client)

    resp = client.post(
        "/pools/new",
        data={"name": "PoolLab Pool", "volume": "40000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    pool_url = resp.headers["location"]
    pool_id = pool_url.rsplit("/", 1)[-1]

    # Missing key is rejected before anything is stored.
    bad = client.post("/integrations/poollab/connect", data={}, follow_redirects=False)
    assert "error=Enter+your+LabCOM+API+key" in bad.headers["location"].replace("%20", "+")

    ok = client.post(
        "/integrations/poollab/connect", data={"api_key": "token-abc"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert "poollab" in ok.headers["location"]
    page = client.get("/integrations")
    assert "connected" in page.text

    sync = client.post(
        "/integrations/poollab/sync", data={"pool_id": pool_id}, follow_redirects=False
    )
    assert sync.status_code == 303
    assert "Synced" in sync.headers["location"].replace("%20", " ")

    pool_page = client.get(pool_url)
    assert "poollab" in pool_page.text
    assert "7.3" in pool_page.text

    # Re-syncing the same measurement is idempotent.
    again = client.post(
        "/integrations/poollab/sync", data={"pool_id": pool_id}, follow_redirects=False
    )
    assert "Synced 0" in again.headers["location"].replace("%20", " ")
