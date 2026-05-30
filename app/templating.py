"""Shared Jinja2 template environment."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .chemistry import Severity

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
templates.env.globals["app_name"] = "Pool Tracking"
