"""Publishing readings to an MQTT broker on a fixed cadence.

A deployment-level feature configured entirely by environment variables
(``MQTT_HOST`` etc. — see ``.env.example``); enabled whenever a host is set.
Every ``MQTT_PUBLISH_INTERVAL_MINUTES`` the scheduler calls
:meth:`MqttPublisher.tick`, which publishes:

- ``{prefix}/{pool_id}/readings`` — every reading stored since the last tick
  (QoS 1, not retained), oldest first. The watermark is the reading row id,
  not ``taken_at``, so history backfilled by a device sync or JSON import is
  published too.
- ``{prefix}/{pool_id}/latest`` — the pool's most recent reading (QoS 1,
  retained), republished whenever it changes, so a subscriber always has
  current state without waiting for the next reading.

Different sources measure different subsets of fields, so each message
carries the pool's last-known value for every measurement as of that
reading's time, each with a ``<field>_measured_at`` timestamp; fields the
pool has never measured are omitted (never null). That way a consumer always
sees the full water picture per message and can tell how fresh each value is.

State is in-memory: publish failures leave it untouched (everything is
retried next tick), and a restart re-seeds the retained topics and resumes
the stream from the current row id (rows stored while the app was down are
reflected in ``latest`` but not replayed on ``readings``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import paho.mqtt.publish as mqtt_publish
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .database import SessionLocal
from .models import Pool, Reading

logger = logging.getLogger("pool_tracking.mqtt")

# Chemistry fields carried in each payload, in the app's canonical units
# (ppm, mV, µS/cm, °C).
_FIELDS = (
    "ph",
    "free_chlorine",
    "total_chlorine",
    "total_alkalinity",
    "cyanuric_acid",
    "calcium_hardness",
    "salt",
    "orp",
    "ec",
    "tds",
    "temperature_c",
)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:  # SQLite returns naive UTC datetimes
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def reading_payload(
    pool: Pool, reading: Reading, state: dict[str, tuple[float, datetime]]
) -> dict:
    """Build a message for ``reading`` carrying the last-known measurements.

    ``state`` maps each field to its most recent (value, measured-at) as of
    this reading. Fields never measured are omitted rather than null.
    """
    payload = {
        "pool_id": pool.id,
        "pool_name": pool.name,
        "reading_id": reading.id,
        "taken_at": _iso_utc(reading.taken_at),
        "source": reading.source.value,
        "external_id": reading.external_id,
    }
    for field in _FIELDS:
        if field in state:
            value, measured_at = state[field]
            payload[field] = value
            payload[f"{field}_measured_at"] = _iso_utc(measured_at)
    return payload


class MqttPublisher:
    """Collects due messages from the DB and pushes them to the broker."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._max_row_id: int | None = None  # stream watermark; None until seeded
        self._latest_sent: dict[int, int] = {}  # pool id -> reading id on /latest
        self._last_attempt_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        if self._last_attempt_at is None:
            return True
        interval = timedelta(minutes=self.settings.mqtt_publish_interval_minutes)
        return now - self._last_attempt_at >= interval

    def tick(self) -> int:
        """Publish anything due; return how many messages went out."""
        self._last_attempt_at = datetime.now(timezone.utc)
        db = SessionLocal()
        try:
            messages, max_row_id, latest_sent = self._collect(db)
        finally:
            db.close()

        if messages:
            try:
                mqtt_publish.multiple(messages, **self._connect_args())
            except Exception as exc:  # noqa: BLE001 - broker/network problems
                # Leave the watermarks untouched so everything retries next tick.
                logger.warning(
                    "MQTT publish to %s:%s failed: %s",
                    self.settings.mqtt_host, self.settings.mqtt_port, exc,
                )
                return 0
            logger.info("Published %d MQTT message(s)", len(messages))

        self._max_row_id = max_row_id
        self._latest_sent.update(latest_sent)
        return len(messages)

    def _connect_args(self) -> dict:
        s = self.settings
        args: dict = {
            "hostname": s.mqtt_host,
            "port": s.mqtt_port,
            "client_id": "pool-tracking",
        }
        if s.mqtt_username:
            args["auth"] = {"username": s.mqtt_username, "password": s.mqtt_password}
        if s.mqtt_use_tls:
            args["tls"] = {}  # default system CA bundle
        return args

    def _collect(self, db: Session) -> tuple[list[dict], int, dict[int, int]]:
        prefix = self.settings.mqtt_topic_prefix.strip("/")
        messages: list[dict] = []

        max_row_id = db.scalar(select(func.max(Reading.id))) or 0

        # Rows stored since the last tick, streamed below. On the first tick
        # there is no watermark yet: skip the stream (don't replay history)
        # and let the retained /latest seeding cover current state.
        new_ids: set[int] = set()
        if self._max_row_id is not None and max_row_id > self._max_row_id:
            new_ids = set(
                db.scalars(select(Reading.id).where(Reading.id > self._max_row_id))
            )

        latest_sent: dict[int, int] = {}
        for pool in db.scalars(select(Pool)).all():
            readings = db.scalars(
                select(Reading)
                .where(Reading.pool_id == pool.id)
                .order_by(Reading.taken_at.asc(), Reading.id.asc())
            ).all()
            if not readings:
                continue
            latest = readings[-1]
            pool_has_new = any(r.id in new_ids for r in readings)
            # Refresh the retained /latest when the newest reading changed, or
            # when a backfilled row may have filled in a missing measurement.
            need_latest = pool_has_new or self._latest_sent.get(pool.id) != latest.id
            if not need_latest:
                continue

            # Walk the pool's history in time order, tracking the most recent
            # value of each field, so every message carries the last-known
            # measurements as of that reading (backfilled rows included).
            state: dict[str, tuple[float, datetime]] = {}
            for reading in readings:
                for field in _FIELDS:
                    value = getattr(reading, field)
                    if value is not None:
                        state[field] = (value, reading.taken_at)
                if reading.id in new_ids:
                    messages.append({
                        "topic": f"{prefix}/{pool.id}/readings",
                        "payload": json.dumps(reading_payload(pool, reading, state)),
                        "qos": 1,
                        "retain": False,
                    })

            messages.append({
                "topic": f"{prefix}/{pool.id}/latest",
                "payload": json.dumps(reading_payload(pool, latest, state)),
                "qos": 1,
                "retain": True,
            })
            latest_sent[pool.id] = latest.id

        return messages, max_row_id, latest_sent
