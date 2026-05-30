"""Tests for the deterministic fallback assessment and chemistry helpers."""
from __future__ import annotations

from app.chemistry import (
    PH,
    Severity,
    cya_range,
    fallback_assessment,
    free_chlorine_range,
)
from app.models import Pool, Reading, SanitizerType, SurfaceType


def _pool(**kw) -> Pool:
    defaults = dict(
        name="Test",
        volume_litres=50000,
        sanitizer=SanitizerType.chlorine,
        surface=SurfaceType.plaster,
        indoor=False,
    )
    defaults.update(kw)
    return Pool(**defaults)


def test_ph_range_contains():
    assert PH.contains(7.4)
    assert not PH.contains(8.1)


def test_free_chlorine_scales_with_cya():
    pool = _pool()
    base = free_chlorine_range(pool, None)
    assert base.contains(2.0)
    high_cya = free_chlorine_range(pool, 80)
    # With CYA 80, ideal FC should rise to ~6 ppm (7.5% of CYA).
    assert high_cya.ideal >= base.ideal


def test_cya_range_varies_by_pool_type():
    assert cya_range(_pool(indoor=True)).high <= 20
    assert cya_range(_pool(sanitizer=SanitizerType.saltwater)).low >= 60


def test_fallback_flags_low_chlorine_as_critical():
    pool = _pool()
    reading = Reading(ph=7.4, free_chlorine=0.1, total_alkalinity=100)
    assessment = fallback_assessment(pool, reading)
    fc = next(r for r in assessment.recommendations if r.parameter == "Free chlorine")
    assert fc.severity is Severity.critical
    assert assessment.overall is Severity.critical


def test_fallback_balanced_water():
    pool = _pool()
    reading = Reading(
        ph=7.4, free_chlorine=3.0, total_alkalinity=100,
        cyanuric_acid=40, calcium_hardness=300,
    )
    assessment = fallback_assessment(pool, reading)
    assert assessment.balanced
    assert all(r.severity is Severity.ok for r in assessment.recommendations)


def test_assessment_sorted_orders_by_severity():
    pool = _pool()
    reading = Reading(ph=8.5, free_chlorine=2.0, total_alkalinity=40)
    assessment = fallback_assessment(pool, reading)
    severities = [r.severity.rank for r in assessment.sorted()]
    assert severities == sorted(severities, reverse=True)
