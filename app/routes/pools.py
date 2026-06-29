"""Dashboard, pool management, readings, and advice."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, chemistry, weather
from ..advice import deserialise_assessment, regenerate_pool_advice
from ..charts import Point, line_chart
from ..config import get_settings
from ..database import get_db
from ..models import (
    Pool,
    PoolAdvice,
    PoolContextNote,
    PoolShape,
    PoolType,
    Reading,
    ReadingSource,
    SanitizerType,
    SurfaceType,
    estimate_volume_litres,
)
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

# Numeric reading columns included in JSON export/import (images excluded).
EXPORT_NUMERIC_FIELDS = [
    "ph", "free_chlorine", "total_chlorine", "total_alkalinity", "cyanuric_acid",
    "calcium_hardness", "salt", "orp", "ec", "tds", "temperature_c",
]

EXPORT_VERSION = 2  # v2 added the pool's dated context log
EXPORT_KIND = "pool-tracking.readings"
MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MB


def _get_owned_pool(db: Session, user_id: int, pool_id: int) -> Pool | None:
    return db.scalar(select(Pool).where(Pool.id == pool_id, Pool.user_id == user_id))


def _local_date(value: datetime, tzname: str | None):
    """Calendar date of ``value`` in ``tzname`` (UTC fallback)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if tzname:
        try:
            return value.astimezone(ZoneInfo(tzname)).date()
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return value.astimezone(timezone.utc).date()


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

    Called on a user action — a new reading or a manual refresh — and shared
    with the twice-daily scheduler via ``advice.regenerate_pool_advice``. Plain
    page views read the stored row instead.
    """
    regenerate_pool_advice(db, pool)


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


def _pool_form_context(user, **extra) -> dict:
    ctx = {
        "user": user,
        "sanitizers": list(SanitizerType),
        "surfaces": list(SurfaceType),
        "pool_types": list(PoolType),
        "shapes": list(PoolShape),
    }
    ctx.update(extra)
    return ctx


@router.get("/pools/new", response_class=HTMLResponse)
def new_pool_form(request: Request, user=Depends(auth.current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "pool_form.html", _pool_form_context(user))


def _parse_float(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except (ValueError, AttributeError):
        return None


def _enum_or_none(enum_cls, value: str):
    try:
        return enum_cls(value) if value.strip() else None
    except ValueError:
        return None


@router.post("/pools/new")
def create_pool(
    request: Request,
    name: str = Form(...),
    volume: float = Form(...),
    volume_unit: str = Form("litres"),
    sanitizer: str = Form("chlorine"),
    surface: str = Form("plaster"),
    indoor: bool = Form(False),
    pool_type: str = Form(""),
    shape: str = Form(""),
    length_m: str = Form(""),
    width_m: str = Form(""),
    avg_depth_m: str = Form(""),
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
        pool_type=_enum_or_none(PoolType, pool_type),
        shape=_enum_or_none(PoolShape, shape),
        length_m=_parse_float(length_m),
        width_m=_parse_float(width_m),
        avg_depth_m=_parse_float(avg_depth_m),
        location_name=place,
        latitude=lat,
        longitude=lon,
        timezone=_timezone_for(lat, lon),
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


def _timezone_for(lat: float | None, lon: float | None) -> str | None:
    """Look up the IANA timezone for a coordinate (best-effort)."""
    if lat is None or lon is None:
        return None
    try:
        return weather.timezone_for(lat, lon)
    except Exception:  # noqa: BLE001 - timezone is a nice-to-have
        return None


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

    # Per-metric in/out-of-range status for the latest reading, so the dashboard
    # tiles can be colour-coded.
    metric_status = _metric_status(pool, list(readings))

    # The pool page shows only today's readings (pool-local date); the full
    # history lives on the analysis page, grouped by week.
    today = _local_date(datetime.now(timezone.utc), pool.timezone)
    todays_readings = [r for r in readings if _local_date(r.taken_at, pool.timezone) == today]

    return templates.TemplateResponse(
        request,
        "pool_detail.html",
        {"user": user,
            "pool": pool,
            "readings": readings,
            "todays_readings": todays_readings,
            "latest": readings[0] if readings else None,
            "assessment": assessment,
            "advice_generated_at": advice_generated_at,
            "metric_status": metric_status,
            "weather": weather_by_date,
            "today": today,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


def _metric_status(pool: Pool, readings: list[Reading]) -> dict[str, str]:
    """Map each measured field of the latest reading to 'ok' or 'out' vs target."""
    if not readings:
        return {}
    latest = readings[0]
    latest_cya = next((r.cyanuric_acid for r in readings if r.cyanuric_acid is not None), None)
    status: dict[str, str] = {}
    for attr in ("ph", "free_chlorine", "total_alkalinity", "cyanuric_acid",
                 "calcium_hardness", "salt", "orp"):
        value = getattr(latest, attr)
        target = _target_for(attr, pool, latest_cya)
        if value is None or target is None:
            continue
        status[attr] = "ok" if target[0] <= value <= target[1] else "out"
    return status


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

    def _local(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if pool.timezone:
            try:
                return value.astimezone(ZoneInfo(pool.timezone))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return value

    charts = []
    for attr, label, unit in READING_CHART_FIELDS:
        series = [
            Point(_local(r.taken_at), getattr(r, attr))
            for r in readings if getattr(r, attr) is not None
        ]
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

    # Full history, newest-first, grouped into weeks (Mon–Sun, pool-local) for an
    # expandable table. The most recent week is rendered open.
    weather_by_date = _weather_for_readings(db, pool, list(readings))
    weeks = _group_by_week(list(reversed(readings)), pool.timezone)

    return templates.TemplateResponse(
        request,
        "analysis.html",
        {
            "user": user,
            "pool": pool,
            "charts": charts,
            "reading_count": len(readings),
            "weeks": weeks,
            "weather": weather_by_date,
        },
    )


def _group_by_week(readings: list[Reading], tzname: str | None) -> list[dict]:
    """Group newest-first ``readings`` into ISO weeks, most recent week first."""
    weeks: list[dict] = []
    for r in readings:
        d = _local_date(r.taken_at, tzname)
        iso = d.isocalendar()  # (ISO year, ISO week, ISO weekday 1=Mon)
        key = (iso[0], iso[1])
        if not weeks or weeks[-1]["key"] != key:
            monday = d - timedelta(days=iso[2] - 1)
            weeks.append({
                "key": key,
                "label": f"Week {iso[1]}, {iso[0]}",
                "start": monday,
                "end": monday + timedelta(days=6),
                "readings": [],
            })
        weeks[-1]["readings"].append(r)
    return weeks


@router.get("/pools/{pool_id}/forecast", response_class=HTMLResponse)
def pool_forecast(
    request: Request,
    pool_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    """Render the forecast dialog body (today + the next days). Lazy-loaded.

    Fetched only when the owner opens the weather dialog, so a normal pool page
    view makes no forecast call and no coordinates leave the server otherwise.
    """
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    if pool.latitude is None or pool.longitude is None:
        return HTMLResponse('<p class="muted">No location set for this pool.</p>')

    days = []
    for idx, summary in enumerate(weather.forecast(pool.latitude, pool.longitude, days=6)):
        try:
            d = datetime.fromisoformat(summary.date).date()
        except ValueError:
            continue
        days.append({
            "summary": summary,
            "label": d.strftime("%a %d %b"),
            "weekday": d.strftime("%A"),
            "is_today": idx == 0,
        })

    return templates.TemplateResponse(
        request, "forecast_dialog.html", {"pool": pool, "days": days}
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


def _context_log_export(pool: Pool) -> list[dict]:
    """Serialise the pool's dated context notes, newest event first."""
    return [
        {"date": n.event_date.isoformat(), "note": n.note}
        for n in sorted(pool.context_notes, key=lambda n: n.event_date, reverse=True)
    ]


def _reading_to_export(r: Reading) -> dict:
    """Serialise one reading to a plain JSON-friendly dict (no image)."""
    out: dict = {"taken_at": r.taken_at.isoformat(), "source": r.source.value}
    if r.external_id:
        out["external_id"] = r.external_id
    for field in EXPORT_NUMERIC_FIELDS:
        val = getattr(r, field)
        if val is not None:
            out[field] = val
    return out


def _norm_dt(value: datetime) -> datetime:
    """Normalise to naive UTC so stored and imported timestamps compare equal."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "pool"


@router.get("/pools/{pool_id}/export")
def export_readings(
    pool_id: int, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    readings = db.scalars(
        select(Reading).where(Reading.pool_id == pool.id).order_by(Reading.taken_at.asc())
    ).all()
    payload = {
        "kind": EXPORT_KIND,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "pool": {
            "name": pool.name,
            "volume_litres": pool.volume_litres,
            "sanitizer": pool.sanitizer.value,
            "surface": pool.surface.value,
            "indoor": pool.indoor,
            "location_name": pool.location_name,
        },
        "readings": [_reading_to_export(r) for r in readings],
        "context_log": _context_log_export(pool),
    }
    filename = f"{_slug(pool.name)}-readings.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _local_iso(value: datetime, tzname: str | None) -> str | None:
    """ISO-8601 timestamp of ``value`` in ``tzname``, or None if no tz."""
    if not tzname:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(ZoneInfo(tzname)).isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return None


# Human-friendly unit labels for the snapshot, so downstream tools/LLMs need
# no domain knowledge to interpret the numbers.
SNAPSHOT_UNITS = {
    "ph": "pH (unitless)", "free_chlorine": "ppm", "total_chlorine": "ppm",
    "total_alkalinity": "ppm", "cyanuric_acid": "ppm", "calcium_hardness": "ppm",
    "salt": "ppm", "orp": "mV", "ec": "µS/cm", "tds": "ppm", "temperature_c": "°C",
}

SNAPSHOT_WINDOW_HOURS = 4


def _pool_spec_dict(pool: Pool) -> dict:
    return {
        "name": pool.name,
        "type": pool.pool_type.value if pool.pool_type else None,
        "shape": pool.shape.value if pool.shape else None,
        "dimensions_m": {
            "length": pool.length_m,
            "width": pool.width_m,
            "avg_depth": pool.avg_depth_m,
        },
        "volume_litres": pool.volume_litres,
        "volume_estimate_litres": estimate_volume_litres(
            pool.shape, pool.length_m, pool.width_m, pool.avg_depth_m
        ),
        "sanitiser": pool.sanitizer.value,
        "surface": pool.surface.value,
        "setting": "indoor" if pool.indoor else "outdoor",
        "location_name": pool.location_name,
        "latitude": pool.latitude,
        "longitude": pool.longitude,
        "timezone": pool.timezone,
        "notes": pool.notes,
        "context_log": _context_log_export(pool),
    }


@router.get("/pools/{pool_id}/snapshot")
def export_snapshot(
    pool_id: int, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=SNAPSHOT_WINDOW_HOURS)
    readings = db.scalars(
        select(Reading)
        .where(Reading.pool_id == pool.id, Reading.taken_at >= cutoff)
        .order_by(Reading.taken_at.desc())
    ).all()

    snapshot_readings = []
    for r in readings:
        item = {
            "taken_at_utc": r.taken_at.astimezone(timezone.utc).isoformat()
            if r.taken_at.tzinfo else r.taken_at.replace(tzinfo=timezone.utc).isoformat(),
            "taken_at_local": _local_iso(r.taken_at, pool.timezone),
            "source": r.source.value,
        }
        if r.external_id:
            item["external_id"] = r.external_id
        for field in EXPORT_NUMERIC_FIELDS:
            val = getattr(r, field)
            if val is not None:
                item[field] = val
        snapshot_readings.append(item)

    payload = {
        "kind": "pool-tracking.snapshot",
        "version": 1,
        "description": (
            f"Snapshot of pool water readings from the last "
            f"{SNAPSHOT_WINDOW_HOURS} hours, with full pool details."
        ),
        "generated_at_utc": now.isoformat(),
        "generated_at_local": _local_iso(now, pool.timezone),
        "timezone": pool.timezone,
        "window_hours": SNAPSHOT_WINDOW_HOURS,
        "units": SNAPSHOT_UNITS,
        "pool": _pool_spec_dict(pool),
        "reading_count": len(snapshot_readings),
        "readings": snapshot_readings,
    }

    stamp = (_local_iso(now, pool.timezone) or now.isoformat())[:16].replace(":", "")
    filename = f"{_slug(pool.name)}-snapshot-{stamp}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/pools/{pool_id}/import")
async def import_readings(
    pool_id: int,
    file: UploadFile = File(...),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    raw = await file.read()
    if not raw:
        return RedirectResponse(f"/pools/{pool.id}?error=The file was empty", status_code=303)
    if len(raw) > MAX_IMPORT_BYTES:
        return RedirectResponse(f"/pools/{pool.id}?error=File too large (max 5 MB)", status_code=303)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return RedirectResponse(f"/pools/{pool.id}?error=Not valid JSON", status_code=303)

    # Accept either our envelope ({"readings": [...]}) or a bare list.
    items = data.get("readings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return RedirectResponse(
            f"/pools/{pool.id}?error=No readings found in that file", status_code=303
        )

    # De-dupe against existing readings on (source, external_id, taken_at).
    seen = {
        (r.source.value, r.external_id, _norm_dt(r.taken_at))
        for r in db.scalars(select(Reading).where(Reading.pool_id == pool.id)).all()
    }

    added = 0
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        taken_raw = item.get("taken_at")
        try:
            taken_at = datetime.fromisoformat(str(taken_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            skipped += 1
            continue

        try:
            source = ReadingSource(item.get("source", "manual"))
        except ValueError:
            source = ReadingSource.manual
        external_id = item.get("external_id") or None

        key = (source.value, external_id, _norm_dt(taken_at))
        if key in seen:
            skipped += 1
            continue

        values: dict[str, float] = {}
        for field in EXPORT_NUMERIC_FIELDS:
            val = item.get(field)
            if val is None:
                continue
            try:
                values[field] = float(val)
            except (TypeError, ValueError):
                continue

        db.add(
            Reading(
                pool_id=pool.id,
                taken_at=taken_at,
                source=source,
                external_id=external_id,
                **values,
            )
        )
        seen.add(key)
        added += 1

    # Import the dated context log too (envelope only; a bare list has no log).
    notes_added = _import_context_log(db, pool, data if isinstance(data, dict) else {})

    db.commit()
    note_suffix = f", {notes_added} context note(s)" if notes_added else ""
    return RedirectResponse(
        f"/pools/{pool.id}?flash=Imported {added} reading(s), "
        f"skipped {skipped} duplicate(s){note_suffix}",
        status_code=303,
    )


def _import_context_log(db: Session, pool: Pool, data: dict) -> int:
    """Add any context-log entries from an import envelope, de-duped. Returns count added."""
    entries = data.get("context_log")
    if not isinstance(entries, list):
        return 0

    seen = {
        (n.event_date.isoformat(), n.note)
        for n in db.scalars(
            select(PoolContextNote).where(PoolContextNote.pool_id == pool.id)
        ).all()
    }

    added = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("note") or "").strip()
        if not text:
            continue
        try:
            when = date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            continue
        key = (when.isoformat(), text)
        if key in seen:
            continue
        db.add(PoolContextNote(pool_id=pool.id, event_date=when, note=text))
        seen.add(key)
        added += 1
    return added


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
    pool.timezone = _timezone_for(lat, lon)
    db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Location updated", status_code=303)


@router.get("/pools/{pool_id}/edit", response_class=HTMLResponse)
def edit_pool_form(
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
    estimate = estimate_volume_litres(
        pool.shape, pool.length_m, pool.width_m, pool.avg_depth_m
    )
    return templates.TemplateResponse(
        request, "pool_edit.html", _pool_form_context(user, pool=pool, volume_estimate=estimate)
    )


@router.post("/pools/{pool_id}/edit")
async def update_pool(
    pool_id: int,
    name: str = Form(...),
    volume: float = Form(...),
    sanitizer: str = Form("chlorine"),
    surface: str = Form("plaster"),
    indoor: bool = Form(False),
    pool_type: str = Form(""),
    shape: str = Form(""),
    length_m: str = Form(""),
    width_m: str = Form(""),
    avg_depth_m: str = Form(""),
    location: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    photo: UploadFile | None = File(None),
    remove_photo: bool = Form(False),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    pool.name = name.strip() or pool.name
    pool.volume_litres = round(volume, 1)
    pool.sanitizer = SanitizerType(sanitizer)
    pool.surface = SurfaceType(surface)
    pool.indoor = indoor
    pool.pool_type = _enum_or_none(PoolType, pool_type)
    pool.shape = _enum_or_none(PoolShape, shape)
    pool.length_m = _parse_float(length_m)
    pool.width_m = _parse_float(width_m)
    pool.avg_depth_m = _parse_float(avg_depth_m)

    # Re-resolve location only when something location-related was provided, so
    # editing other fields doesn't wipe an existing location.
    if location.strip() or latitude.strip() or longitude.strip():
        lat, lon, place = _resolve_location(location, latitude, longitude)
        if lat is not None and lon is not None:
            pool.latitude = lat
            pool.longitude = lon
            pool.location_name = place
            pool.timezone = _timezone_for(lat, lon)

    error = _apply_pool_photo(pool, photo, remove_photo)
    if error:
        return RedirectResponse(f"/pools/{pool.id}?error={error}", status_code=303)

    db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Details updated", status_code=303)


def _apply_pool_photo(pool: Pool, photo: UploadFile | None, remove: bool) -> str | None:
    """Save/remove a pool cover photo. Returns an error message, or None on success."""
    raw = None
    media_type = (photo.content_type or "").lower() if photo else ""
    # Starlette gives an empty UploadFile (no filename) when the field is left blank.
    has_upload = bool(photo and photo.filename)

    old = pool.image_path
    if remove or has_upload:
        if old:
            old_path = _safe_upload_path(old)
            if old_path is not None:
                old_path.unlink(missing_ok=True)
            pool.image_path = None

    if has_upload:
        if media_type not in SUPPORTED_IMAGE_TYPES:
            return "Pool photo must be a JPEG, PNG, WebP or HEIC image."
        raw = photo.file.read()
        if len(raw) > MAX_IMAGE_BYTES:
            return "Pool photo is too large (max 8 MB)."
        pool.image_path = _save_upload(photo, raw)
    return None


@router.get("/pools/{pool_id}/image")
def pool_image(
    pool_id: int, user=Depends(auth.current_user), db: Session = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None or not pool.image_path:
        return RedirectResponse("/", status_code=303)
    path = _safe_upload_path(pool.image_path)
    if path is None:
        return RedirectResponse(f"/pools/{pool.id}", status_code=303)
    return FileResponse(path)


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


@router.post("/pools/{pool_id}/context")
def add_context_note(
    pool_id: int,
    event_date: str = Form(""),
    note: str = Form(""),
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    """Add a dated context-log entry spanning the pool's life.

    We don't regenerate advice here: the new note is picked up by the next
    scheduled run, the next reading, or a manual Refresh, so adding context
    doesn't trigger a Claude call.
    """
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)

    text = note.strip()
    if not text:
        return RedirectResponse(
            f"/pools/{pool.id}?error=Add a note describing what happened", status_code=303
        )

    try:
        when = date.fromisoformat(event_date.strip()) if event_date.strip() else None
    except ValueError:
        when = None
    if when is None:
        when = _local_date(datetime.now(timezone.utc), pool.timezone)

    db.add(PoolContextNote(pool_id=pool.id, event_date=when, note=text))
    db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Context note added", status_code=303)


@router.post("/pools/{pool_id}/context/{note_id}/delete")
def delete_context_note(
    pool_id: int,
    note_id: int,
    user=Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    pool = _get_owned_pool(db, user.id, pool_id)
    if pool is None:
        return RedirectResponse("/", status_code=303)
    note = db.scalar(
        select(PoolContextNote).where(
            PoolContextNote.id == note_id, PoolContextNote.pool_id == pool.id
        )
    )
    if note is not None:
        db.delete(note)
        db.commit()
    return RedirectResponse(f"/pools/{pool.id}?flash=Context note deleted", status_code=303)


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
