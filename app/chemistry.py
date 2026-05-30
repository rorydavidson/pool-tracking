"""Shared types and reference data for pool water chemistry.

The actual advice is generated on the fly by Claude (see ``advice.py``) using
the full pool specification and reading history. This module holds the data
structures both sides share, the published target ranges (handed to Claude as
reference and used by the deterministic fallback), and a simple rule-based
fallback assessment used when no Anthropic API key is configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Pool, Reading, SanitizerType


class Severity(str, Enum):
    ok = "ok"
    low = "low"  # informational nudge
    warning = "warning"  # out of range, act soon
    critical = "critical"  # unsafe / risk of damage, act now

    @property
    def rank(self) -> int:
        return {"ok": 0, "low": 1, "warning": 2, "critical": 3}[self.value]


@dataclass(frozen=True)
class Range:
    low: float
    high: float
    ideal: float

    def contains(self, v: float) -> bool:
        return self.low <= v <= self.high


@dataclass
class Recommendation:
    parameter: str  # e.g. "pH"
    severity: Severity
    message: str  # human-readable explanation
    action: str | None = None  # what to add / do, with an estimated dose


@dataclass
class Assessment:
    summary: str = ""
    recommendations: list[Recommendation] = field(default_factory=list)
    # Ordered, concrete next steps to rebalance the water (most urgent first).
    next_steps: list[str] = field(default_factory=list)
    # Where the advice came from: "claude" or "fallback".
    source: str = "fallback"

    @property
    def overall(self) -> Severity:
        if not self.recommendations:
            return Severity.ok
        return max((r.severity for r in self.recommendations), key=lambda s: s.rank)

    @property
    def balanced(self) -> bool:
        return self.overall in (Severity.ok, Severity.low)

    def sorted(self) -> list[Recommendation]:
        """Most urgent first, preserving insertion order within a severity."""
        return [
            r
            for _, r in sorted(
                enumerate(self.recommendations),
                key=lambda pair: (-pair[1].severity.rank, pair[0]),
            )
        ]


# --- Published target ranges (residential pools) ---------------------------
# These are passed to Claude as reference data and used by the fallback.

PH = Range(7.2, 7.6, 7.4)
TOTAL_ALKALINITY = Range(80, 120, 100)  # ppm
CALCIUM_HARDNESS = Range(200, 400, 300)  # ppm
ORP = Range(650, 750, 720)  # mV
SALT = Range(2700, 3400, 3200)  # ppm, for saltwater chlorinators


def free_chlorine_range(pool: Pool, cya: float | None) -> Range:
    """Effective free-chlorine target; scales with stabiliser (CYA) when present."""
    if pool.sanitizer == SanitizerType.bromine:
        return Range(3.0, 5.0, 4.0)  # bromine equivalent
    if cya and cya > 0:
        ideal = max(2.0, round(0.075 * cya, 1))  # ~7.5% of CYA keeps chlorine active
        return Range(round(ideal * 0.7, 1), round(ideal * 1.6, 1), ideal)
    return Range(1.0, 3.0, 2.0)


def cya_range(pool: Pool) -> Range:
    if pool.indoor:
        return Range(0, 20, 10)
    if pool.sanitizer == SanitizerType.saltwater:
        return Range(60, 80, 70)
    return Range(30, 50, 40)


# A compact reference table Claude can ground its advice in.
TARGET_REFERENCE = {
    "pH": PH,
    "total_alkalinity": TOTAL_ALKALINITY,
    "calcium_hardness": CALCIUM_HARDNESS,
    "orp": ORP,
    "salt": SALT,
}


# --- Deterministic fallback ------------------------------------------------
# Used only when Claude is unavailable (no ANTHROPIC_API_KEY, or an API error).
# Intentionally simple: flag out-of-range parameters without dosing maths.

def fallback_assessment(pool: Pool, reading: Reading) -> Assessment:
    a = Assessment(
        source="fallback",
        summary=(
            "AI advice is unavailable (no ANTHROPIC_API_KEY configured), so this is a "
            "basic range check. Set an API key for tailored, dosed recommendations."
        ),
    )

    def check(name: str, value: float | None, rng: Range, unit: str = "") -> None:
        if value is None:
            return
        if rng.contains(value):
            a.recommendations.append(
                Recommendation(name, Severity.ok, f"{name} {value:g}{unit} is in range "
                               f"({rng.low:g}–{rng.high:g}{unit}).")
            )
        else:
            direction = "low" if value < rng.low else "high"
            a.recommendations.append(
                Recommendation(
                    name, Severity.warning,
                    f"{name} {value:g}{unit} is {direction} (target {rng.low:g}–"
                    f"{rng.high:g}{unit}).",
                )
            )

    check("pH", reading.ph, PH)
    check("Total alkalinity", reading.total_alkalinity, TOTAL_ALKALINITY, " ppm")
    check("Calcium hardness", reading.calcium_hardness, CALCIUM_HARDNESS, " ppm")
    check("Cyanuric acid", reading.cyanuric_acid, cya_range(pool), " ppm")
    check(
        "Free chlorine",
        reading.free_chlorine,
        free_chlorine_range(pool, reading.cyanuric_acid),
        " ppm",
    )
    if pool.sanitizer == SanitizerType.saltwater:
        check("Salt", reading.salt, SALT, " ppm")
    check("ORP", reading.orp, ORP, " mV")

    if reading.free_chlorine is not None and reading.free_chlorine < 0.5:
        for r in a.recommendations:
            if r.parameter == "Free chlorine":
                r.severity = Severity.critical
    return a
