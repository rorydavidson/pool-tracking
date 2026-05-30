"""Historical weather lookup via Open-Meteo (no API key required).

Used to correlate pool chemistry changes with the weather on each reading's
day — e.g. hot, sunny days burn off chlorine faster; heavy rain dilutes the
water and shifts pH. Results are cached per location/date in ``WeatherDay`` so
we don't re-hit the API for the same days.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Pool, WeatherDay

logger = logging.getLogger("pool_tracking.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,uv_index_max,windspeed_10m_max,weathercode"

# Human-readable summaries for WMO weather codes (condensed).
_WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}


@dataclass
class WeatherSummary:
    date: str
    temp_max_c: float | None
    temp_min_c: float | None
    precipitation_mm: float | None
    uv_index_max: float | None
    wind_max_kmh: float | None
    weather_code: int | None

    @property
    def description(self) -> str:
        if self.weather_code is None:
            return ""
        return _WMO.get(self.weather_code, "")

    @classmethod
    def from_row(cls, row: WeatherDay) -> "WeatherSummary":
        return cls(
            date=row.date,
            temp_max_c=row.temp_max_c,
            temp_min_c=row.temp_min_c,
            precipitation_mm=row.precipitation_mm,
            uv_index_max=row.uv_index_max,
            wind_max_kmh=row.wind_max_kmh,
            weather_code=row.weather_code,
        )


def geocode(place: str) -> tuple[float, float, str] | None:
    """Resolve a place name to (latitude, longitude, resolved_name)."""
    try:
        resp = httpx.get(
            GEOCODE_URL, params={"name": place, "count": 1, "language": "en"}, timeout=15
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        logger.warning("Geocoding failed for %r", place)
        return None
    if not results:
        return None
    r = results[0]
    label = ", ".join(filter(None, [r.get("name"), r.get("admin1"), r.get("country")]))
    return float(r["latitude"]), float(r["longitude"]), label


def timezone_for(lat: float, lon: float) -> str | None:
    """Return the IANA timezone name for a coordinate, via Open-Meteo."""
    try:
        resp = httpx.get(
            FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon,
                "timezone": "auto", "forecast_days": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        tz = resp.json().get("timezone")
    except (httpx.HTTPError, ValueError):
        logger.warning("Timezone lookup failed for (%s, %s)", lat, lon)
        return None
    return tz if isinstance(tz, str) and tz else None


def _round(coord: float) -> float:
    return round(coord, 2)  # ~1 km granularity for cache reuse


def weather_for_dates(
    db: Session, pool: Pool, dates: set[dt.date]
) -> dict[str, WeatherSummary]:
    """Return weather keyed by ISO date for the given dates, fetching misses.

    Returns an empty dict if the pool has no location set.
    """
    if pool.latitude is None or pool.longitude is None or not dates:
        if not dates:
            return {}
        logger.info(
            "Skipping weather for pool %s: no location set (lat/lon missing)", pool.id
        )
        return {}

    lat, lon = _round(pool.latitude), _round(pool.longitude)
    wanted = {d.isoformat() for d in dates}

    rows = db.scalars(
        select(WeatherDay).where(
            WeatherDay.latitude == lat,
            WeatherDay.longitude == lon,
            WeatherDay.date.in_(wanted),
        )
    ).all()
    found = {row.date: WeatherSummary.from_row(row) for row in rows}

    missing = sorted(wanted - set(found))
    if missing:
        fetched = _fetch_range(lat, lon, missing)
        if not fetched:
            logger.info(
                "Weather fetch returned no days for pool %s (%s, %s) dates=%s",
                pool.id, lat, lon, missing,
            )
        for iso, summary in fetched.items():
            db.add(
                WeatherDay(
                    latitude=lat,
                    longitude=lon,
                    date=iso,
                    temp_max_c=summary.temp_max_c,
                    temp_min_c=summary.temp_min_c,
                    precipitation_mm=summary.precipitation_mm,
                    uv_index_max=summary.uv_index_max,
                    wind_max_kmh=summary.wind_max_kmh,
                    weather_code=summary.weather_code,
                )
            )
            found[iso] = summary
        if fetched:
            db.commit()
    return found


def _fetch_range(lat: float, lon: float, iso_dates: list[str]) -> dict[str, WeatherSummary]:
    """Fetch daily weather covering the span of ``iso_dates`` from Open-Meteo.

    Recent dates (within ~90 days) come from the forecast API's ``past_days``;
    older dates use the historical archive. We request the full min..max span
    once and pick out the days we need.
    """
    start = min(iso_dates)
    end = max(iso_dates)
    today = dt.date.today()
    end_date = dt.date.fromisoformat(end)
    use_archive = (today - end_date).days > 5  # archive lags a few days

    try:
        if use_archive:
            resp = httpx.get(
                ARCHIVE_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "start_date": start, "end_date": end,
                    "daily": _DAILY_VARS, "timezone": "auto",
                },
                timeout=20,
            )
        else:
            past_days = min(92, max(1, (today - dt.date.fromisoformat(start)).days + 1))
            resp = httpx.get(
                FORECAST_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": _DAILY_VARS, "timezone": "auto",
                    "past_days": past_days, "forecast_days": 1,
                },
                timeout=20,
            )
        resp.raise_for_status()
        daily = resp.json().get("daily") or {}
    except (httpx.HTTPError, ValueError):
        logger.warning("Weather fetch failed for (%s, %s) %s..%s", lat, lon, start, end)
        return {}

    times = daily.get("time") or []
    wanted = set(iso_dates)
    out: dict[str, WeatherSummary] = {}
    for i, iso in enumerate(times):
        if iso not in wanted:
            continue

        def _at(key: str):
            seq = daily.get(key) or []
            return seq[i] if i < len(seq) else None

        code = _at("weathercode")
        out[iso] = WeatherSummary(
            date=iso,
            temp_max_c=_at("temperature_2m_max"),
            temp_min_c=_at("temperature_2m_min"),
            precipitation_mm=_at("precipitation_sum"),
            uv_index_max=_at("uv_index_max"),
            wind_max_kmh=_at("windspeed_10m_max"),
            weather_code=int(code) if code is not None else None,
        )
    return out
