"""Dashboard, pool management, readings, and advice."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, chemistry, weather
from ..advice import deserialise_assessment, generate_advice, serialise_assessment
from ..charts import Point, line_chart
from ..config import get_settings
from ..database import get_db
from ..models import Pool, PoolAdvice, Reading, ReadingSource, SanitizerType, SurfaceType
from ..templating import templates
from ..vision import MAX_IMAGE_BYTES, SUPPORTED_IMAGE_TYPES, VisionUnavailable, read_test_strip

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
    ("ec", "EC (µS/cm)"),
    ("tds", "TDS (ppm)"),
    ("temperature_c", "Temperature (°C)"),
]

# (attribute, chart title, unit suffix) for the analysis page trend charts.
READING_CHART_FIELDS = [
    ("ph", "pH", ""),
    ("free_chlorine", "Free chlorine", " ppm"),
    ("total_chlorine", "Total chlorine", " ppm"),
    ("total_alkalinity", "Total alkalinity", " ppm"),
    ("cyanuric_acid", "Cyanuric acid", " ppm"),
    ("calcium_hardness", "Calcium hardness", " ppm"),
    ("salt", "Salt", " ppm"),
    ("orp", "ORP", " mV"),
    ("ec", "EC", " µS/cm"),
    ("tds", "TDS", " ppm"),
    ("temperature_c", "Temperature", " °C"),
]


def _get_owned_pool(db: Session, user_id: int, pool_id: int) -> Pool | None:
    return db.scalar(select(Pool).where(Pool.id == pool_id, Pool.user_id == user_id))


def _weather_for_readings(db: Session, pool: Pool, readings: list[Reading]) -> dict:
    """Best-effort weather lookup for the dates in ``readings``."""
    if not readings:
        return {}
    dates = {r.taken_at.date() for r in readings}
    try:
        return weather.weather_for_dates(db, pool, dates)
    except Exception:  # noqa: BLE001 - weather is a nice-to-have, never fatal
        return {}


def _regenerate_advice(db: Session, pool: Pool) -> None:
    """Generate fresh advice for ``pool`` and persist it (upserting one row).

    This is the only place that calls the (expensive) advice generator on a
    user action — a new reading or a manual refresh. Plain page views read the
    stored row instead.
    """
    readings = db.scalars(
        select(Reading).where(Reading.pool_id == pool.id).order_by(Reading.taken_at.desc())
    ).all()
    if not readings:
        return

    weather_by_date = _weather_for_readings(db, pool, list(readings))
    assessment = generate_advice(pool, list(readings), weather_by_date, notes=pool.notes)
    payload = serialise_assessment(assessment)

    if pool.advice is None:
        db.add(PoolAdvice(pool_id=pool.id, reading_id=readings[0].id, payload=payload))
    else:
        pool.advice.payload = payload
        pool.advice.reading_id = readings[0].id
        pool.advice.generated_at = datetime.now(timezone.utc)
    db.commit()


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
    weather_by_date = _weather_for_readings(db, pool, list(readings))

    # Show the stored advice; page views never regenerate it (that happens on a
    # new reading or an explicit refresh).
    assessment = deserialise_assessment(pool.advice.payload) if pool.advice else None
    advice_generated_at = pool.advice.generated_at if pool.advice else None

    return templates.TemplateResponse(
        request,
        "pool_detail.html",
        {"user": user,
            "pool": pool,
            "readings": readings,
            "latest": readings[0] if readings else None,
            "assessment": assessment,
            "advice_generated_at": advice_generated_at,
            "weather": weather_by_date,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


def _target_for(attr: str, pool: Pool, latest_cya: float | None):
    """Return a (low, high) target band for a parameter, or None."""
    if attr == "ph":
        return (chemistry.PH.low, chemistry.PH.high)
    if attr == "total_alkalinity":
        return (chemistry.TOTAL_ALKALINITY.low, chemistry.TOTAL_ALKALINITY.high)
    if attr == "calcium_hardness":
        return (chemistry.CALCIUM_HARDNESS.low, chemistry.CALCIUM_HARDNESS.high)
    if attr == "orp":
        return (chemistry.ORP.low, chemistry.ORP.high)
    if attr == "cyanuric_acid":
        r = chemistry.cya_range(pool)
        return (r.low, r.high)
    if attr == "free_chlorine":
        r = chemistry.free_chlorine_range(pool, latest_cya)
        return (r.low, r.high)
    if attr == "salt" and pool.sanitizer == SanitizerType.saltwater:
        return (chemistry.SALT.low, chemistry.SALT.high)
    return None


@router.get("/pools/{pool_id}/analysis", response_class=HTMLResponse)
def pool_analysis(
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
        select(Reading).where(Reading.pool_id == pool.id).order_by(Reading.taken_at.asc())
    ).all()

    latest_cya = next(
        (r.cyanuric_acid for r in reversed(readings) if r.cyanuric_acid is not None), None
    )

    charts = []
    for attr, label, unit in READING_CHART_FIELDS:
        series = [Point(r.taken_at, getattr(r, attr)) for r in readings if getattr(r, attr) is not None]
        if not series:
            continue
        target = _target_for(attr, pool, latest_cya)
        charts.append(
            {
                "label": label,
                "unit": unit,
                "latest": series[-1].value,
                "count": len(series),
                "target": target,
                "svg": line_chart(series, target=target, unit=unit),
            }
        )

    return templates.TemplateResponse(
        request,
        "analysis.html",
        {"user": user, "pool": pool, "charts": charts, "reading_count": len(readings)},
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
    return _render_reading_form(request, user, pool)


def _render_reading_form(
    request: Request,
    user,
    pool: Pool,
    *,
    prefill: dict | None = None,
    image_path: str | None = None,
    scan: dict | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "reading_form.html",
        {
            "user": user,
            "pool": pool,
            "fields": READING_FIELDS,
            "prefill": prefill or {},
            "image_path": image_path,
            "scan": scan,
        },
    )


def _save_upload(upload: UploadFile, raw: bytes) -> str:
    """Store an uploaded image under the uploads dir; return its filename."""
    media_type = (upload.content_type or "").lower()
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/heic": ".heic", "image/heif": ".heif"}.get(media_type, ".img")
    filename = f"{uuid.uuid4().hex}{ext}"
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / filename).write_bytes(raw)
    return filename


def _safe_upload_path(filename: str):
    """Resolve a stored upload filename to a path, rejecting traversal."""
    settings = get_settings()
    base = os.path.basename(filename)  # strip any directory components
    candidate = settings.uploads_dir / base
    if base != filename or not candidate.is_file():
        return None
    return candidate


@router.post("/pools/{pool_id}/readings/scan")
async def scan_strip(
    request: Request,
    pool_id: int,
    photo: UploadFile = File(...),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    media_type = (photo.content_type or "").lower()
    if media_type not in SUPPORTED_IMAGE_TYPES:
        return _render_reading_form(
            request, user, pool,
            scan={"error": "Please upload a JPEG, PNG, WebP or HEIC image."},
        )

    raw = await photo.read()
    if not raw:
        return _render_reading_form(
            request, user, pool, scan={"error": "That file was empty."}
        )
    if len(raw) > MAX_IMAGE_BYTES:
        return _render_reading_form(
            request, user, pool,
            scan={"error": "Image is too large (max 8 MB)."},
        )

    filename = _save_upload(photo, raw)

    try:
        reading = read_test_strip(raw, SUPPORTED_IMAGE_TYPES[media_type])
    except VisionUnavailable as exc:
        # Keep the stored image so the user can attach it after typing values.
        return _render_reading_form(
            request, user, pool, image_path=filename, scan={"error": str(exc)}
        )

    prefill = {k: v for k, v in reading.model_dump().items()
               if k in {f for f, _ in READING_FIELDS} and v is not None}
    return _render_reading_form(
        request, user, pool,
        prefill=prefill,
        image_path=filename,
        scan={"confidence": reading.confidence, "notes": reading.notes,
              "count": len(prefill)},
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

    # Attach a previously uploaded strip photo, if one came through the scan flow.
    image_path = None
    raw_image = (form.get("image_path") or "").strip()
    if raw_image and _safe_upload_path(raw_image) is not None:
        image_path = os.path.basename(raw_image)

    # "Other information" is pool-level context; persist it on the pool so it
    # carries across readings and feeds future advice.
    notes = (form.get("notes") or "").strip()
    pool.notes = notes or None

    reading = Reading(
        pool_id=pool.id, source=ReadingSource.manual, image_path=image_path, **values
    )
    db.add(reading)
    db.commit()

    # A new reading is a deliberate action with fresh data, so refresh advice.
    _regenerate_advice(db, pool)
    return RedirectResponse(f"/pools/{pool.id}", status_code=303)


@router.get("/pools/{pool_id}/readings/{reading_id}/image")
def reading_image(
    pool_id: int,
    reading_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    reading = db.scalar(
        select(Reading).where(Reading.id == reading_id, Reading.pool_id == pool.id)
    )
    if reading is None or not reading.image_path:
        return RedirectResponse(f"/pools/{pool.id}", status_code=303)
    path = _safe_upload_path(reading.image_path)
    if path is None:
        return RedirectResponse(f"/pools/{pool.id}", status_code=303)
    return FileResponse(path)


@router.post("/pools/{pool_id}/readings/{reading_id}/delete")
def delete_reading(
    pool_id: int,
    reading_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    reading = db.scalar(
        select(Reading).where(Reading.id == reading_id, Reading.pool_id == pool.id)
    )
    if reading is None:
        return RedirectResponse(f"/pools/{pool.id}", status_code=303)

    # Remove the attached strip photo from disk, if any.
    if reading.image_path:
        path = _safe_upload_path(reading.image_path)
        if path is not None:
            path.unlink(missing_ok=True)

    db.delete(reading)
    db.flush()

    # Advice is generated manually, so we don't regenerate here — but if this was
    # the last reading, the stored advice no longer has anything to describe.
    remaining = db.scalar(
        select(Reading).where(Reading.pool_id == pool.id).limit(1)
    )
    if remaining is None:
        advice = db.scalar(select(PoolAdvice).where(PoolAdvice.pool_id == pool.id))
        if advice is not None:
            db.delete(advice)

    db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Reading deleted", status_code=303)


@router.post("/pools/{pool_id}/location")
def set_location(
    pool_id: int,
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    lat, lon, place = _resolve_location(location, latitude, longitude)
    if lat is None or lon is None:
        return RedirectResponse(
            f"/pools/{pool.id}?error=Could not find that location", status_code=303
        )
    pool.latitude = lat
    pool.longitude = lon
    pool.location_name = place
    db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Location updated", status_code=303)


@router.post("/pools/{pool_id}/advice/refresh")
def refresh_advice(
    pool_id: int, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    _regenerate_advice(db, pool)
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
