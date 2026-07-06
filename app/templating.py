"""Shared Jinja2 template environment."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from .chemistry import Severity

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def fmt_local(
    value: dt.datetime | None,
    tzname: str | None,
    fmt: str = "%Y-%m-%d %H:%M",
    label: bool = True,
) -> str:
    """Format a (UTC) datetime in ``tzname``, appending a short tz label.

    Falls back to UTC when no/invalid timezone is given. Naive datetimes are
    assumed to be UTC (that's how the app stores them). Pass ``label=False``
    to omit the tz label (e.g. for date-only formats).
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    tz = dt.timezone.utc
    if tzname:
        try:
            tz = ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError):
            tz = dt.timezone.utc
    local = value.astimezone(tz)
    if not label:
        return local.strftime(fmt)
    tz_label = local.tzname() or ("UTC" if tz is dt.timezone.utc else tzname or "UTC")
    return f"{local.strftime(fmt)} {tz_label}"

# Badge colour per severity, used by templates.
_SEVERITY_CLASS = {
    Severity.ok: "ok",
    Severity.low: "low",
    Severity.warning: "warning",
    Severity.critical: "critical",
}


def severity_class(sev: Severity) -> str:
    return _SEVERITY_CLASS.get(sev, "warning")


# Emoji per advice parameter, matched on keywords so it copes with whatever
# wording Claude uses (e.g. "Free chlorine", "Total Cl", "Stabiliser (CYA)").
_PARAM_ICONS = [
    ("alkalin", "🧪"),
    ("chlorine", "🫧"),
    ("bromine", "🫧"),
    ("cyanuric", "🛡️"),
    ("stabilis", "🛡️"),
    ("stabiliz", "🛡️"),
    ("calcium", "🪨"),
    ("hardness", "🪨"),
    ("salt", "🧂"),
    ("orp", "⚡"),
    ("redox", "⚡"),
    ("temp", "🌡️"),
    ("tds", "💠"),
    ("conduct", "💠"),  # EC / conductivity
    ("ph", "⚗️"),  # keep last: 'ph' also appears inside other words
]


def param_icon(name: str | None) -> str:
    """Return a relevant emoji for an advice parameter name (💧 default)."""
    text = (name or "").lower()
    for needle, icon in _PARAM_ICONS:
        if needle in text:
            return icon
    return "💧"


# Emoji per WMO weather code group, for the weather card and forecast dialog.
def weather_icon(code: int | None) -> str:
    if code is None:
        return "🌡️"
    if code == 0:
        return "☀️"
    if code in (1, 2):
        return "🌤️"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if code in (51, 53, 55, 61, 63, 80, 81):
        return "🌧️"
    if code in (65, 82):
        return "⛈️"
    if code in (71, 73, 75):
        return "❄️"
    if code in (95, 96, 99):
        return "⛈️"
    return "🌡️"


templates.env.globals["severity_class"] = severity_class
templates.env.globals["fmt_local"] = fmt_local
templates.env.globals["param_icon"] = param_icon
templates.env.globals["weather_icon"] = weather_icon
templates.env.globals["app_name"] = "Pool Tracking"
