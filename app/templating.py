"""Shared Jinja2 template environment."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from .chemistry import Severity

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def fmt_local(value: dt.datetime | None, tzname: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a (UTC) datetime in ``tzname``, appending a short tz label.

    Falls back to UTC when no/invalid timezone is given. Naive datetimes are
    assumed to be UTC (that's how the app stores them).
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
    label = local.tzname() or ("UTC" if tz is dt.timezone.utc else tzname or "UTC")
    return f"{local.strftime(fmt)} {label}"

# Badge colour per severity, used by templates.
_SEVERITY_CLASS = {
    Severity.ok: "ok",
    Severity.low: "low",
    Severity.warning: "warning",
    Severity.critical: "critical",
}


def severity_class(sev: Severity) -> str:
    return _SEVERITY_CLASS.get(sev, "warning")


templates.env.globals["severity_class"] = severity_class
templates.env.globals["fmt_local"] = fmt_local
templates.env.globals["app_name"] = "Pool Tracking"
