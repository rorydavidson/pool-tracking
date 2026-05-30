"""Dashboard, pool management, readings, and advice."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, weather
from ..advice import generate_advice
from ..database import get_db
from ..models import Pool, Reading, ReadingSource, SanitizerType, SurfaceType
from ..templating import templates

router = APIRouter()

# Optional reading fields rendered on the form and parsed from it.
READING_FIELDS = [
    ("ph", "pH"),
    ("free_chlorine", "Free chlorine (ppm)"),
    ("total_chlorine", "Total chlorine (ppm)"),
    ("total_alkalinity", "Total alkalinity (ppm)"),
    ("cyanuric_acid", "Cyanuric acid / stabiliser (ppm)"),
    ("calcium_hardness", "Calcium hardness (ppm)"),
    ("salt", "Salt (ppm)"),
    ("orp", "ORP (mV)"),
    ("tds", "TDS (ppm)"),
    ("temperature_c", "Temperature (°C)"),
]


def _get_owned_pool(db: Session, user_id: int, pool_id: int) -> Pool | None:
    return db.scalar(select(Pool).where(Pool.id == pool_id, Pool.user_id == user_id))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(auth.current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pools = db.scalars(select(Pool).where(Pool.user_id == user.id).order_by(Pool.name)).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "pools": pools}
    )


@router.get("/pools/new", response_class=HTMLResponse)
def new_pool_form(request: Request, user=Depends(auth.current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "pool_form.html",
        {"user": user,
            "sanitizers": list(SanitizerType),
            "surfaces": list(SurfaceType),
        },
    )


@router.post("/pools/new")
def create_pool(
    request: Request,
    name: str = Form(...),
    volume: float = Form(...),
    volume_unit: str = Form("litres"),
    sanitizer: str = Form("chlorine"),
    surface: str = Form("plaster"),
    indoor: bool = Form(False),
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    litres = volume * 3.78541 if volume_unit == "gallons" else volume

    lat, lon, place = _resolve_location(location, latitude, longitude)

    pool = Pool(
        user_id=user.id,
        name=name.strip(),
        volume_litres=round(litres, 1),
        sanitizer=SanitizerType(sanitizer),
        surface=SurfaceType(surface),
        indoor=indoor,
        location_name=place,
        latitude=lat,
        longitude=lon,
    )
    db.add(pool)
    db.commit()
    return RedirectResponse(f"/pools/{pool.id}", status_code=303)


def _resolve_location(
    location: str, latitude: str, longitude: str
) -> tuple[float | None, float | None, str | None]:
    """Prefer explicit lat/lon; otherwise geocode the place name."""
    place = location.strip() or None
    try:
        lat = float(latitude) if latitude.strip() else None
        lon = float(longitude) if longitude.strip() else None
    except ValueError:
        lat = lon = None
    if (lat is None or lon is None) and place:
        geo = weather.geocode(place)
        if geo:
            lat, lon, place = geo
    return lat, lon, place


@router.get("/pools/{pool_id}", response_class=HTMLResponse)
def pool_detail(
    request: Request,
    pool_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    readings = db.scalars(
        select(Reading).where(Reading.pool_id == pool.id).order_by(Reading.taken_at.desc())
    ).all()

    # Correlate readings with the weather on those days (best-effort).
    weather_by_date: dict = {}
    if readings:
        dates = {r.taken_at.date() for r in readings}
        try:
            weather_by_date = weather.weather_for_dates(db, pool, dates)
        except Exception:  # noqa: BLE001 - weather is a nice-to-have, never fatal
            weather_by_date = {}

    assessment = generate_advice(pool, list(readings), weather_by_date) if readings else None
    return templates.TemplateResponse(
        request,
        "pool_detail.html",
        {"user": user,
            "pool": pool,
            "readings": readings,
            "latest": readings[0] if readings else None,
            "assessment": assessment,
            "weather": weather_by_date,
        },
    )


@router.get("/pools/{pool_id}/readings/new", response_class=HTMLResponse)
def new_reading_form(
    request: Request,
    pool_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "reading_form.html",
        {"user": user, "pool": pool, "fields": READING_FIELDS},
    )


@router.post("/pools/{pool_id}/readings/new")
async def create_reading(
    request: Request,
    pool_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    values: dict[str, float] = {}
    for field, _label in READING_FIELDS:
        raw = (form.get(field) or "").strip()
        if raw:
            try:
                values[field] = float(raw)
            except ValueError:
                continue

    reading = Reading(pool_id=pool.id, source=ReadingSource.manual, **values)
    db.add(reading)
    db.commit()
    return RedirectResponse(f"/pools/{pool.id}", status_code=303)


@router.post("/pools/{pool_id}/delete")
def delete_pool(
    pool_id: int, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is not None:
        db.delete(pool)
        db.commit()
    return RedirectResponse("/", status_code=303)
