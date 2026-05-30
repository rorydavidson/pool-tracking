"""Connect, verify, sync, and disconnect third-party device accounts."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..integrations import ProviderError, get_client
from ..integrations.base import DeviceMeasurement
from ..models import Pool, Provider, ProviderCredential, Reading, ReadingSource
from ..security import decrypt_json, encrypt_json
from ..templating import templates

router = APIRouter()

_PROVIDER_SOURCE = {
    Provider.aiper: ReadingSource.aiper,
    Provider.blueriiot: ReadingSource.blueriiot,
}


def _credentials_by_provider(db: Session, user_id: int) -> dict[Provider, ProviderCredential]:
    rows = db.scalars(
        select(ProviderCredential).where(ProviderCredential.user_id == user_id)
    ).all()
    return {row.provider: row for row in rows}


@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    creds = _credentials_by_provider(db, user.id)
    pools = db.scalars(select(Pool).where(Pool.user_id == user.id).order_by(Pool.name)).all()
    return templates.TemplateResponse(
        request,
        "integrations.html",
        {"user": user,
            "providers": list(Provider),
            "creds": creds,
            "pools": pools,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/integrations/{provider}/connect")
def connect(
    provider: str,
    email: str = Form(...),
    password: str = Form(...),
    region: str = Form("eu"),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    prov = Provider(provider)

    payload = {"email": email.strip(), "password": password}
    if region.strip():
        payload["region"] = region.strip()

    # Verify the credentials before saving so we don't store bad logins silently.
    try:
        get_client(prov, payload).verify()
    except ProviderError as exc:
        return RedirectResponse(f"/integrations?error={exc}", status_code=303)

    existing = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user.id, ProviderCredential.provider == prov
        )
    )
    blob = encrypt_json(payload)
    if existing:
        existing.secret_blob = blob
        existing.last_sync_error = None
    else:
        db.add(ProviderCredential(user_id=user.id, provider=prov, secret_blob=blob))
    db.commit()
    return RedirectResponse(f"/integrations?flash={prov.value} connected", status_code=303)


@router.post("/integrations/{provider}/disconnect")
def disconnect(
    provider: str, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    prov = Provider(provider)
    row = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user.id, ProviderCredential.provider == prov
        )
    )
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(f"/integrations?flash={prov.value} disconnected", status_code=303)


@router.post("/integrations/{provider}/sync")
def sync(
    provider: str,
    pool_id: int = Form(...),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    prov = Provider(provider)
    pool = db.scalar(
        select(Pool).where(Pool.id == pool_id, Pool.user_id == user.id)
    )
    cred = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user.id, ProviderCredential.provider == prov
        )
    )
    if pool is None or cred is None:
        return RedirectResponse("/integrations?error=Pool or connection not found", status_code=303)

    try:
        client = get_client(prov, decrypt_json(cred.secret_blob))
        measurements = client.latest_measurements()
    except ProviderError as exc:
        cred.last_sync_error = str(exc)
        db.commit()
        return RedirectResponse(f"/integrations?error={exc}", status_code=303)

    added = _store_measurements(db, pool, prov, measurements)
    cred.last_sync_at = datetime.now(timezone.utc)
    cred.last_sync_error = None
    db.commit()
    return RedirectResponse(
        f"/integrations?flash=Synced {added} new reading(s) from {prov.value}", status_code=303
    )


def _store_measurements(
    db: Session, pool: Pool, provider: Provider, measurements: list[DeviceMeasurement]
) -> int:
    source = _PROVIDER_SOURCE[provider]
    added = 0
    for m in measurements:
        # De-dupe on (pool, source, external_id, taken_at).
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
