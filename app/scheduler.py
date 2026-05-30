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

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .models import Pool, ProviderCredential
from .sync_service import sync_credential

logger = logging.getLogger("pool_tracking.scheduler")

# How often the loop wakes to look for due devices. The actual per-device cadence
# is governed by AUTO_SYNC_INTERVAL_HOURS; this just bounds the check latency.
_CHECK_PERIOD_SECONDS = 600  # 10 minutes
_STARTUP_DELAY_SECONDS = 45  # let the app settle before the first pass

_task: asyncio.Task | None = None


def _due_credential_ids(interval_hours: float) -> list[int]:
    """Return ids of enabled credentials due for a sync (own short-lived session)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=interval_hours)
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


async def _run_loop() -> None:
    settings = get_settings()
    interval = settings.auto_sync_interval_hours
    logger.info("Device auto-sync scheduler started (interval=%sh)", interval)
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            due = await asyncio.to_thread(_due_credential_ids, interval)
            for cred_id in due:
                await asyncio.to_thread(_sync_one, cred_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Auto-sync scheduler tick failed")
        await asyncio.sleep(_CHECK_PERIOD_SECONDS)


def start_scheduler() -> None:
    """Start the background loop, unless disabled via config."""
    global _task
    settings = get_settings()
    if settings.auto_sync_interval_hours <= 0:
        logger.info("Device auto-sync disabled (AUTO_SYNC_INTERVAL_HOURS=0)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run_loop())
