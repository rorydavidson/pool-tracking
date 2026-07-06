"""Unit tests for the MQTT publisher (no broker)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.database import SessionLocal
from app.models import Pool, Reading, ReadingSource, User
from app.mqtt_publisher import MqttPublisher, reading_payload

T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def pool(db, request):
    user = User(email=f"mqtt-{request.node.name}@example.com")
    db.add(user)
    db.flush()
    pool = Pool(user_id=user.id, name="MQTT Pool", volume_litres=30000)
    db.add(pool)
    db.commit()
    return pool


def _add_reading(db, pool, taken_at, **fields):
    reading = Reading(
        pool_id=pool.id, taken_at=taken_at, source=ReadingSource.manual, **fields
    )
    db.add(reading)
    db.commit()
    return reading


def _publisher(monkeypatch, sent, fail=None):
    """A publisher against a fake broker; `sent` collects messages, `fail` raises."""
    settings = Settings(mqtt_host="broker.local", mqtt_topic_prefix="test_mqtt")

    def fake_multiple(messages, **kwargs):
        if fail and fail[0]:
            raise OSError("connection refused")
        sent.append((messages, kwargs))

    monkeypatch.setattr("app.mqtt_publisher.mqtt_publish.multiple", fake_multiple)
    return MqttPublisher(settings)


def _topics(batch, pool):
    return [m["topic"] for m in batch if f"/{pool.id}/" in m["topic"]]


def test_payload_has_stable_keys(db, pool):
    r = _add_reading(db, pool, T0, ph=7.2, free_chlorine=1.5)
    payload = reading_payload(pool, r)
    assert payload["pool_name"] == "MQTT Pool"
    assert payload["taken_at"] == "2026-07-01T09:00:00+00:00"
    assert payload["source"] == "manual"
    assert payload["ph"] == 7.2
    # Unmeasured fields are present and null so consumers get stable keys.
    assert payload["salt"] is None
    assert "temperature_c" in payload


def test_first_tick_seeds_retained_latest_only(monkeypatch, db, pool):
    _add_reading(db, pool, T0, ph=7.0)
    latest = _add_reading(db, pool, T0 + timedelta(hours=1), ph=7.4)
    sent = []
    pub = _publisher(monkeypatch, sent)
    pub.tick()
    (batch, kwargs) = sent[0]
    mine = [m for m in batch if f"/{pool.id}/" in m["topic"]]
    # No stream replay of history, just the retained snapshot.
    assert [m["topic"] for m in mine] == [f"test_mqtt/{pool.id}/latest"]
    assert mine[0]["retain"] is True
    assert json.loads(mine[0]["payload"])["reading_id"] == latest.id
    assert kwargs["hostname"] == "broker.local"
    assert "auth" not in kwargs  # no credentials configured


def test_new_readings_stream_and_latest_update(monkeypatch, db, pool):
    _add_reading(db, pool, T0, ph=7.0)
    sent = []
    pub = _publisher(monkeypatch, sent)
    pub.tick()  # seed

    # Nothing new: no messages at all on the next tick.
    assert pub.tick() == 0

    r1 = _add_reading(db, pool, T0 + timedelta(hours=1), ph=7.2)
    r2 = _add_reading(db, pool, T0 + timedelta(hours=2), ph=7.4)
    pub.tick()
    batch = sent[-1][0]
    stream = [m for m in batch if m["topic"] == f"test_mqtt/{pool.id}/readings"]
    assert [json.loads(m["payload"])["reading_id"] for m in stream] == [r1.id, r2.id]
    assert all(m["retain"] is False for m in stream)
    latest = [m for m in batch if m["topic"] == f"test_mqtt/{pool.id}/latest"]
    assert json.loads(latest[0]["payload"])["reading_id"] == r2.id


def test_backfilled_history_is_streamed(monkeypatch, db, pool):
    _add_reading(db, pool, T0, ph=7.0)
    sent = []
    pub = _publisher(monkeypatch, sent)
    pub.tick()  # seed

    # A device sync imports an *older* reading: still published (id watermark),
    # but the retained latest is untouched (it's not the newest by taken_at).
    old = _add_reading(db, pool, T0 - timedelta(days=30), ph=6.8)
    pub.tick()
    batch = sent[-1][0]
    topics = _topics(batch, pool)
    assert topics == [f"test_mqtt/{pool.id}/readings"]
    assert json.loads(batch[0]["payload"])["reading_id"] == old.id


def test_failed_publish_retries_next_tick(monkeypatch, db, pool):
    _add_reading(db, pool, T0, ph=7.1)
    sent = []
    fail = [True]
    pub = _publisher(monkeypatch, sent, fail=fail)
    assert pub.tick() == 0  # broker down; watermarks untouched
    assert sent == []
    fail[0] = False
    assert pub.tick() >= 1  # same messages go out on the retry
    assert f"test_mqtt/{pool.id}/latest" in _topics(sent[0][0], pool)


def test_due_respects_configured_interval():
    settings = Settings(mqtt_host="broker.local", mqtt_publish_interval_minutes=15)
    pub = MqttPublisher(settings)
    now = datetime.now(timezone.utc)
    assert pub.due(now)  # never published yet
    pub._last_attempt_at = now
    assert not pub.due(now + timedelta(minutes=10))
    assert pub.due(now + timedelta(minutes=15))


def test_disabled_without_host():
    assert Settings(mqtt_host="").mqtt_enabled is False
    assert Settings(mqtt_host="b", mqtt_publish_interval_minutes=0).mqtt_enabled is False
    assert Settings(mqtt_host="b").mqtt_enabled is True


def test_auth_and_tls_connect_args():
    settings = Settings(
        mqtt_host="b", mqtt_username="u", mqtt_password="p", mqtt_use_tls=True
    )
    args = MqttPublisher(settings)._connect_args()
    assert args["auth"] == {"username": "u", "password": "p"}
    assert args["tls"] == {}
