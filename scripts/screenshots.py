"""Render each page template to a PNG with representative data.

Used to preview the UI design without a browser: WeasyPrint rasterises the
real templates + CSS, PyMuPDF converts the resulting PDF to PNG.

Run:  python scripts/screenshots.py   (outputs to ./screenshots/)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import fitz  # PyMuPDF
from weasyprint import HTML

from app.chemistry import Assessment, Recommendation, Severity
from app.models import (
    Provider,
    Reading,
    ReadingSource,
    SanitizerType,
    SurfaceType,
)
from app.templating import templates
from app.weather import WeatherSummary

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app" / "static" / "style.css").read_text()
OUT = ROOT / "screenshots"
OUT.mkdir(exist_ok=True)

# Render at a comfortable desktop-ish width on a single tall page per view.
PAGE_CSS = "@page { size: 1040px 1500px; margin: 0; } body { background: #f3f6f8; }"


def render(name: str, context: dict, out_name: str, page_height: int = 1500) -> None:
    context.setdefault("request", SimpleNamespace(query_params={}))
    html = templates.env.get_template(name).render(**context)
    # Inline the stylesheet (replace the <link>) and set the page box.
    page_css = f"@page {{ size: 1040px {page_height}px; margin: 0; }} body {{ background: #f3f6f8; }}"
    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css">',
        f"<style>{CSS}\n{page_css}</style>",
    )
    pdf_bytes = HTML(string=html, base_url=str(ROOT)).write_pdf()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6))  # crisper than 1x
    path = OUT / out_name
    pix.save(path)
    print(f"wrote {path}  ({pix.width}x{pix.height})")
    doc.close()


def main() -> None:
    user = SimpleNamespace(email="rory@example.com")

    # --- login ---
    render("login.html", {"user": None}, "01_login.png", page_height=620)

    # --- dashboard ---
    pools = [
        SimpleNamespace(
            id=1, name="Back garden pool", volume_litres=52000,
            sanitizer=SanitizerType.saltwater, indoor=False,
            readings=[1, 2, 3, 4, 5, 6, 7],
        ),
        SimpleNamespace(
            id=2, name="Spa / hot tub", volume_litres=1500,
            sanitizer=SanitizerType.bromine, indoor=True, readings=[1, 2],
        ),
    ]
    render("dashboard.html", {"user": user, "pools": pools}, "02_dashboard.png", page_height=560)

    # --- new pool form ---
    render(
        "pool_form.html",
        {"user": user, "sanitizers": list(SanitizerType), "surfaces": list(SurfaceType)},
        "03_pool_form.png",
        page_height=900,
    )

    # --- pool detail with advice + weather ---
    pool = SimpleNamespace(
        id=1, name="Back garden pool", volume_litres=52000,
        sanitizer=SanitizerType.saltwater, surface=SurfaceType.plaster,
        indoor=False, location_name="Brisbane, Queensland, Australia",
        latitude=-27.47, longitude=153.02,
    )

    def mk_reading(days_ago, **vals):
        base = dict(
            ph=None, free_chlorine=None, total_chlorine=None, total_alkalinity=None,
            cyanuric_acid=None, calcium_hardness=None, salt=None, orp=None,
            tds=None, temperature_c=None,
        )
        base.update(vals)
        return SimpleNamespace(
            taken_at=dt.datetime(2026, 5, 30) - dt.timedelta(days=days_ago),
            source=ReadingSource.aiper if days_ago % 2 else ReadingSource.manual,
            **base,
        )

    readings = [
        mk_reading(0, ph=7.8, free_chlorine=0.4, total_alkalinity=70,
                   cyanuric_acid=75, calcium_hardness=260, salt=2600, orp=590, temperature_c=29),
        mk_reading(2, ph=7.6, free_chlorine=1.6, total_alkalinity=80,
                   cyanuric_acid=72, salt=2900, orp=680, temperature_c=27),
        mk_reading(5, ph=7.5, free_chlorine=2.4, total_alkalinity=90,
                   cyanuric_acid=70, salt=3100, orp=720, temperature_c=24),
        mk_reading(8, ph=7.4, free_chlorine=2.8, total_alkalinity=100,
                   cyanuric_acid=68, salt=3200, orp=735, temperature_c=22),
    ]

    weather = {
        readings[0].taken_at.date().isoformat(): WeatherSummary(
            readings[0].taken_at.date().isoformat(), 34.0, 23.0, 0.0, 11.0, 18.0, 0),
        readings[1].taken_at.date().isoformat(): WeatherSummary(
            readings[1].taken_at.date().isoformat(), 31.0, 21.0, 2.0, 9.0, 22.0, 2),
        readings[2].taken_at.date().isoformat(): WeatherSummary(
            readings[2].taken_at.date().isoformat(), 26.0, 19.0, 24.0, 4.0, 35.0, 63),
        readings[3].taken_at.date().isoformat(): WeatherSummary(
            readings[3].taken_at.date().isoformat(), 25.0, 18.0, 1.0, 6.0, 15.0, 1),
    }

    assessment = Assessment(
        source="claude",
        summary=(
            "Your salt pool's sanitiser has fallen behind: free chlorine is critically "
            "low and ORP confirms weak sanitation, most likely because the last few hot, "
            "high-UV days (34°C, UV 11) burned chlorine off faster than the cell could "
            "replace it. Raise chlorine now, nudge pH and alkalinity up, and top up the salt."
        ),
        recommendations=[
            Recommendation(
                "Free chlorine", Severity.critical,
                "Free chlorine 0.4 ppm is critically low for a stabilised pool (CYA 75 wants "
                "~5-6 ppm). The water is effectively unsanitised — keep swimmers out.",
                "Raise free chlorine by ~5 ppm: add about 370 g of calcium hypochlorite (70%) "
                "or 2.1 L of liquid chlorine (12.5%) to this 52,000 L pool, then run the pump "
                "and re-test. Boost the chlorinator output while the weather stays hot.",
            ),
            Recommendation(
                "pH", Severity.warning,
                "pH 7.8 is high, which further weakens the little chlorine you have and can "
                "cause scale.",
                "Add about 520 mL of muriatic acid (31%) over a return jet, then re-test.",
            ),
            Recommendation(
                "Salt", Severity.warning,
                "Salt 2600 ppm is below the 2700-3400 range, so the chlorinator can't keep up.",
                "Add roughly 31 kg of pool salt and let it dissolve before re-testing.",
            ),
            Recommendation(
                "Total alkalinity", Severity.low,
                "Total alkalinity 70 ppm is a touch low, letting pH drift.",
                "Add about 1.5 kg of sodium bicarbonate to bring it toward 100 ppm.",
            ),
            Recommendation(
                "Cyanuric acid", Severity.ok,
                "Stabiliser 75 ppm is in the ideal 60-80 range for a salt pool.",
            ),
        ],
    )

    render(
        "pool_detail.html",
        {
            "user": user, "pool": pool, "readings": readings,
            "latest": readings[0], "assessment": assessment, "weather": weather,
        },
        "04_pool_detail.png",
        page_height=2050,
    )

    # --- reading form ---
    from app.routes.pools import READING_FIELDS

    render(
        "reading_form.html",
        {"user": user, "pool": pool, "fields": READING_FIELDS},
        "05_reading_form.png",
        page_height=1180,
    )

    # --- integrations ---
    creds = {
        Provider.blueriiot: SimpleNamespace(
            last_sync_at=dt.datetime(2026, 5, 30, 6, 15), last_sync_error=None
        )
    }
    render(
        "integrations.html",
        {
            "user": user, "providers": list(Provider), "creds": creds, "pools": pools,
            "flash": "blueriiot connected", "error": None,
            "request": SimpleNamespace(query_params={"flash": "blueriiot connected"}),
        },
        "06_integrations.png",
        page_height=1080,
    )


if __name__ == "__main__":
    main()
