"""Connect, verify, sync, and disconnect third-party device accounts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..database import get_db
from ..integrations import ProviderError, get_client
from ..models import Pool, Provider, ProviderCredential
from ..security import encrypt_json
from ..sync_service import sync_credential
from ..templating import templates

router = APIRouter()


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
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "integrations.html",
        {"user": user,
            "providers": list(Provider),
            "creds": creds,
            "pools": pools,
            "auto_sync_hours": settings.auto_sync_interval_hours,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/integrations/{provider}/connect")
def connect(
    provider: str,
    email: str = Form(""),
    password: str = Form(""),
    region: str = Form("eu"),
    api_key: str = Form(""),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    prov = Provider(provider)

    # PoolLab uses a LabCOM API token; the other providers use account logins.
    if prov == Provider.poollab:
        if not api_key.strip():
            return RedirectResponse(
                "/integrations?error=Enter your LabCOM API key", status_code=303
            )
        payload = {"api_key": api_key.strip()}
    else:
        if not email.strip() or not password:
            return RedirectResponse(
                "/integrations?error=Enter your email and password", status_code=303
            )
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

    added, error = sync_credential(db, cred, pool)
    if error:
        return RedirectResponse(f"/integrations?error={error}", status_code=303)
    return RedirectResponse(
        f"/integrations?flash=Synced {added} new reading(s) from {prov.value}", status_code=303
    )


@router.post("/integrations/{provider}/autosync")
def set_autosync(
    provider: str,
    enabled: bool = Form(False),
    pool_id: str = Form(""),
    interval_hours: str = Form(""),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    prov = Provider(provider)
    cred = db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.user_id == user.id, ProviderCredential.provider == prov
        )
    )
    if cred is None:
        return RedirectResponse("/integrations?error=Connection not found", status_code=303)

    target = None
    if pool_id.strip():
        target = db.scalar(
            select(Pool).where(Pool.id == int(pool_id), Pool.user_id == user.id)
        )
    if enabled and target is None:
        return RedirectResponse(
            "/integrations?error=Choose a pool to auto-sync into", status_code=303
        )

    # Blank = use the global default cadence; otherwise store the chosen hours.
    interval = None
    if interval_hours.strip():
        try:
            value = float(interval_hours)
            interval = value if value > 0 else None
        except ValueError:
            interval = None

    cred.auto_sync_enabled = enabled
    cred.auto_sync_pool_id = target.id if target else None
    cred.auto_sync_interval_hours = interval
    db.commit()
    if enabled:
        effective = interval or get_settings().auto_sync_interval_hours
        msg = f"Auto-sync on for {prov.value} into {target.name} every {effective:g}h"
    else:
        msg = f"Auto-sync off for {prov.value}"
    return RedirectResponse(f"/integrations?flash={msg}", status_code=303)
