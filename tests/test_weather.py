"""Tests for weather caching and correlation wiring (no network)."""
from __future__ import annotations

import datetime as dt

from app import weather
from app.database import SessionLocal
from app.models import Pool, SanitizerType, SurfaceType, User, WeatherDay


def _pool_with_location(db, lat=27.47, lon=153.02):
    user = User(email=f"w{lat}@example.com")
    db.add(user)
    db.flush()
    pool = Pool(
        user_id=user.id,
        name="Loc Pool",
        volume_litres=40000,
        sanitizer=SanitizerType.chlorine,
        surface=SurfaceType.plaster,
        latitude=lat,
        longitude=lon,
    )
    db.add(pool)
    db.commit()
    return pool


def test_weather_skipped_without_location():
    db = SessionLocal()
    try:
        pool = Pool(
            name="No Loc", volume_litres=1000,
            sanitizer=SanitizerType.chlorine, surface=SurfaceType.plaster,
        )
        assert weather.weather_for_dates(db, pool, {dt.date(2026, 5, 1)}) == {}
    finally:
        db.close()


def test_weather_served_from_cache_without_network():
    db = SessionLocal()
    try:
        pool = _pool_with_location(db)
        # Pre-seed the cache so no HTTP call is needed.
        db.add(
            WeatherDay(
                latitude=round(pool.latitude, 2),
                longitude=round(pool.longitude, 2),
                date="2026-05-01",
                temp_max_c=31.0,
                temp_min_c=19.0,
                precipitation_mm=0.0,
                uv_index_max=9.0,
                wind_max_kmh=12.0,
                weather_code=0,
            )
        )
        db.commit()

        result = weather.weather_for_dates(db, pool, {dt.date(2026, 5, 1)})
        assert "2026-05-01" in result
        summary = result["2026-05-01"]
        assert summary.temp_max_c == 31.0
        assert summary.description == "Clear"
    finally:
        db.close()
