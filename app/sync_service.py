"""Pulling readings from device providers, shared by the manual sync route and
the background scheduler.

Storing is idempotent: readings are de-duped on
``(pool, source, external_id, taken_at)`` (also enforced by a DB constraint), so
manual and automatic syncs can overlap freely.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .integrations import ProviderError, get_client
from .integrations.base import DeviceMeasurement
from .models import Pool, Provider, ProviderCredential, Reading, ReadingSource
from .security import decrypt_json

PROVIDER_SOURCE = {
    Provider.aiper: ReadingSource.aiper,
    Provider.blueriiot: ReadingSource.blueriiot,
    Provider.poollab: ReadingSource.poollab,
}


def store_measurements(
    db: Session, pool: Pool, provider: Provider, measurements: list[DeviceMeasurement]
) -> int:
    """Insert any measurements not already stored; return how many were added."""
    source = PROVIDER_SOURCE[provider]
    added = 0
    for m in measurements:
        exists = db.scalar(
            select(Reading).where(
                Reading.pool_id == pool.id,
                Reading.source == source,
                Reading.external_id == m.external_id,
                Reading.taken_at == m.taken_at,
            )
        )
        if exists:
            continue
        db.add(
            Reading(
                pool_id=pool.id,
                source=source,
                taken_at=m.taken_at,
                external_id=m.external_id,
                ph=m.ph,
                free_chlorine=m.free_chlorine,
                total_chlorine=m.total_chlorine,
                total_alkalinity=m.total_alkalinity,
                cyanuric_acid=m.cyanuric_acid,
                calcium_hardness=m.calcium_hardness,
                salt=m.salt,
                orp=m.orp,
                ec=m.ec,
                tds=m.tds,
                temperature_c=m.temperature_c,
            )
        )
        added += 1
    return added


def sync_credential(
    db: Session, cred: ProviderCredential, pool: Pool
) -> tuple[int, str | None]:
    """Pull and store readings for one credential into ``pool``.

    Returns ``(added, error)``. ``last_sync_at`` is updated on both success and
    failure so the scheduler backs off to the normal interval rather than
    retrying a flaky/offline device on every tick.
    """
    now = datetime.now(timezone.utc)
    try:
        client = get_client(cred.provider, decrypt_json(cred.secret_blob))
        measurements = client.latest_measurements()
    except ProviderError as exc:
        cred.last_sync_at = now
        cred.last_sync_error = str(exc)
        db.commit()
        return 0, str(exc)

    added = store_measurements(db, pool, cred.provider, measurements)
    cred.last_sync_at = now
    cred.last_sync_error = None
    db.commit()
    return added, None
