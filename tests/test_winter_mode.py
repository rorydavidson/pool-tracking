"""Winter / closed mode: the pool flag and the stretched device sync cadence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Pool, Provider, ProviderCredential
from app.scheduler import (
    WINTER_SYNC_INTERVAL_HOURS,
    _due_credential_ids,
    effective_sync_interval,
)


def _cred(interval: float | None = None) -> ProviderCredential:
    return ProviderCredential(provider=Provider.poollab, auto_sync_interval_hours=interval)


def test_effective_interval_unchanged_outside_winter():
    pool = Pool(winter_mode=False)
    assert effective_sync_interval(_cred(), pool, 1.0) == 1.0
    assert effective_sync_interval(_cred(3), pool, 1.0) == 3


def test_winter_mode_stretches_to_twice_daily():
    pool = Pool(winter_mode=True)
    assert effective_sync_interval(_cred(), pool, 1.0) == WINTER_SYNC_INTERVAL_HOURS
    assert effective_sync_interval(_cred(6), pool, 1.0) == WINTER_SYNC_INTERVAL_HOURS


def test_winter_mode_keeps_a_longer_chosen_interval():
    assert effective_sync_interval(_cred(24), Pool(winter_mode=True), 1.0) == 24


def test_winter_mode_does_not_enable_disabled_sync():
    assert effective_sync_interval(_cred(), Pool(winter_mode=True), 0) == 0


def _create_pool(client, name: str) -> int:
    resp = client.post(
        "/pools/new",
        data={"name": name, "volume": "30000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rstrip("/").split("/")[-1])


def _edit(client, pool_id: int, **extra) -> None:
    data = {"name": "Winter Pool", "volume": "30000", **extra}
    resp = client.post(f"/pools/{pool_id}/edit", data=data, follow_redirects=False)
    assert resp.status_code == 303


def test_toggle_winter_mode_via_edit(logged_in_client):
    from app.database import SessionLocal

    pool_id = _create_pool(logged_in_client, "Winter Pool")

    _edit(logged_in_client, pool_id, winter_mode="true")
    page = logged_in_client.get(f"/pools/{pool_id}")
    assert "hero no-photo winter" in page.text
    assert "Winter mode · pool closed" in page.text
    with SessionLocal() as db:
        assert db.get(Pool, pool_id).winter_mode is True

    # Unchecked checkboxes aren't submitted, so a plain save turns it off.
    _edit(logged_in_client, pool_id)
    with SessionLocal() as db:
        assert db.get(Pool, pool_id).winter_mode is False


def test_scheduler_skips_winter_pool_synced_recently(logged_in_client):
    from app.database import SessionLocal

    summer_id = _create_pool(logged_in_client, "Summer Pool")
    winter_id = _create_pool(logged_in_client, "Closed Pool")
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

    with SessionLocal() as db:
        db.get(Pool, winter_id).winter_mode = True
        user_id = db.get(Pool, summer_id).user_id
        # One credential per provider per user, so use two providers.
        creds = [
            ProviderCredential(
                user_id=user_id, provider=provider, secret_blob="x",
                auto_sync_enabled=True, auto_sync_pool_id=pool_id,
                last_sync_at=two_hours_ago,
            )
            for provider, pool_id in (
                (Provider.aiper, summer_id), (Provider.blueriiot, winter_id)
            )
        ]
        db.add_all(creds)
        db.commit()
        summer_cred, winter_cred = (c.id for c in creds)

    try:
        due = _due_credential_ids(1.0)
        assert summer_cred in due
        assert winter_cred not in due
        page = logged_in_client.get("/integrations").text
        assert "every 12h into" in page and "(❄ winter mode)" in page
    finally:
        with SessionLocal() as db:
            for cid in (summer_cred, winter_cred):
                db.delete(db.get(ProviderCredential, cid))
            db.commit()


def test_fallback_advice_mentions_winter_mode():
    from app.chemistry import fallback_assessment
    from app.models import Reading

    reading = Reading(taken_at=datetime.now(timezone.utc), ph=7.4)
    assert "winter mode" not in fallback_assessment(Pool(winter_mode=False), [reading]).summary
    assert "winter mode" in fallback_assessment(Pool(winter_mode=True), [reading]).summary


def test_toggling_winter_mode_regenerates_advice(logged_in_client):
    pool_id = _create_pool(logged_in_client, "Advice Pool")
    logged_in_client.post(f"/pools/{pool_id}/readings/new", data={"ph": "7.4"})
    assert "Advice is tailored for a closed pool" not in logged_in_client.get(
        f"/pools/{pool_id}"
    ).text

    _edit(logged_in_client, pool_id, winter_mode="true")
    page = logged_in_client.get(f"/pools/{pool_id}").text
    assert "Advice is tailored for a closed pool" in page
    # The stored advice itself was rewritten for the new mode.
    assert "The pool is in winter mode" in page
