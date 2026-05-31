"""Background scheduler that periodically syncs auto-enabled devices.

A single in-process asyncio loop (no extra dependency, no separate worker) wakes
on a fixed cadence and syncs any credential whose auto-sync is enabled and whose
last sync is older than the configured interval. Each device sync is blocking
(HTTP / MQTT), so it runs in a worker thread with its own DB session.

If you run multiple app workers, each would run this loop; that's safe because
reading storage is idempotent, but only one is needed, so prefer a single worker.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from .advice import regenerate_pool_advice
from .config import get_settings
from .database import SessionLocal
from .models import Pool, PoolAdvice, ProviderCredential, Reading
from .sync_service import sync_credential

logger = logging.getLogger("pool_tracking.scheduler")

# How often the loop wakes to look for due devices. The actual per-device cadence
# is governed by AUTO_SYNC_INTERVAL_HOURS; this just bounds the check latency.
_CHECK_PERIOD_SECONDS = 600  # 10 minutes
_STARTUP_DELAY_SECONDS = 45  # let the app settle before the first pass

# Local times of day at which each pool's advice is automatically regenerated.
_ADVICE_HOURS = (7, 19)

_task: asyncio.Task | None = None


def _due_credential_ids(default_interval_hours: float) -> list[int]:
    """Return ids of enabled credentials due for a sync (own short-lived session).

    Each device's cadence is its own ``auto_sync_interval_hours`` when set,
    otherwise the global default.
    """
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ProviderCredential).where(
                ProviderCredential.auto_sync_enabled.is_(True),
                ProviderCredential.auto_sync_pool_id.is_not(None),
            )
        ).all()
        due = []
        for cred in rows:
            interval = cred.auto_sync_interval_hours or default_interval_hours
            if interval <= 0:
                continue
            cutoff = now - timedelta(hours=interval)
            last = cred.last_sync_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or last <= cutoff:
                due.append(cred.id)
        return due
    finally:
        db.close()


def _sync_one(cred_id: int) -> None:
    """Sync a single credential in its own session (runs in a worker thread)."""
    db = SessionLocal()
    try:
        cred = db.get(ProviderCredential, cred_id)
        if cred is None or not cred.auto_sync_enabled or cred.auto_sync_pool_id is None:
            return
        pool = db.get(Pool, cred.auto_sync_pool_id)
        if pool is None:
            return
        added, error = sync_credential(db, cred, pool)
        if error:
            logger.info("Auto-sync %s for pool %s: %s", cred.provider.value, pool.id, error)
        else:
            logger.info(
                "Auto-sync %s for pool %s: %d new reading(s)",
                cred.provider.value, pool.id, added,
            )
    except Exception:  # noqa: BLE001 - never let one device kill the loop
        logger.exception("Auto-sync failed for credential %s", cred_id)
    finally:
        db.close()


def _last_advice_boundary(now_utc: datetime, tzname: str | None) -> datetime:
    """Most recent 07:00/19:00 local instant at or before ``now_utc``, as UTC.

    Resilient to downtime: whatever scheduled slot has most recently passed is
    returned, so a pool whose advice predates it is due regardless of how many
    ticks were missed.
    """
    try:
        tz = ZoneInfo(tzname) if tzname else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local = now_utc.astimezone(tz)
    candidates = []
    for day_offset in (0, -1):
        day = (local + timedelta(days=day_offset)).date()
        for hour in _ADVICE_HOURS:
            cand = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            if cand <= local:
                candidates.append(cand.astimezone(timezone.utc))
    return max(candidates)


def _due_advice_pool_ids() -> list[int]:
    """Pools with readings whose stored advice is older than the latest slot."""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        due = []
        for pool in db.scalars(select(Pool)).all():
            has_reading = db.scalar(
                select(Reading.id).where(Reading.pool_id == pool.id).limit(1)
            )
            if has_reading is None:
                continue
            boundary = _last_advice_boundary(now, pool.timezone)
            advice = db.scalar(select(PoolAdvice).where(PoolAdvice.pool_id == pool.id))
            generated = advice.generated_at if advice else None
            if generated is not None and generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            if generated is None or generated < boundary:
                due.append(pool.id)
        return due
    finally:
        db.close()


def _run_advice_one(pool_id: int) -> None:
    """Regenerate one pool's advice in its own session (runs in a worker thread)."""
    db = SessionLocal()
    try:
        pool = db.get(Pool, pool_id)
        if pool is None:
            return
        if regenerate_pool_advice(db, pool):
            logger.info("Scheduled advice regenerated for pool %s", pool_id)
    except Exception:  # noqa: BLE001 - never let one pool kill the loop
        logger.exception("Scheduled advice failed for pool %s", pool_id)
    finally:
        db.close()


async def _run_loop() -> None:
    settings = get_settings()
    interval = settings.auto_sync_interval_hours
    advice_enabled = bool(settings.anthropic_api_key)
    logger.info(
        "Scheduler started (device sync=%sh, twice-daily advice=%s)",
        interval if interval > 0 else "off",
        "on" if advice_enabled else "off (no ANTHROPIC_API_KEY)",
    )
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if interval > 0:
                due = await asyncio.to_thread(_due_credential_ids, interval)
                for cred_id in due:
                    await asyncio.to_thread(_sync_one, cred_id)
            if advice_enabled:
                for pool_id in await asyncio.to_thread(_due_advice_pool_ids):
                    await asyncio.to_thread(_run_advice_one, pool_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(_CHECK_PERIOD_SECONDS)


def start_scheduler() -> None:
    """Start the background loop.

    The loop drives both device auto-sync (when ``AUTO_SYNC_INTERVAL_HOURS`` > 0)
    and twice-daily advice regeneration (when an Anthropic key is set). If both
    are off it still runs but does nothing each tick.
    """
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run_loop())
