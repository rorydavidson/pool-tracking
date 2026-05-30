"""Tiny dependency-free SVG line charts for the analysis page.

Everything is rendered server-side as inline SVG — no JavaScript, no external
libraries — so it works offline and keeps the app's privacy-first, server-
rendered design. Each chart plots one parameter over time with an optional
shaded target band.
"""
from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass

# Canvas geometry (the SVG scales to its container via width:100%).
_W, _H = 320.0, 120.0
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 8.0, 8.0, 12.0, 20.0
_PLOT_W = _W - _PAD_L - _PAD_R
_PLOT_H = _H - _PAD_T - _PAD_B


@dataclass
class Point:
    when: dt.datetime
    value: float


def _fmt(v: float) -> str:
    """Compact number formatting (no trailing zeros)."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def line_chart(
    points: list[Point],
    *,
    target: tuple[float, float] | None = None,
    unit: str = "",
) -> str:
    """Return an inline SVG line chart for a chronological list of points."""
    pts = [p for p in points if p.value is not None]
    if not pts:
        return '<p class="muted small">No data yet.</p>'

    values = [p.value for p in pts]
    lo_v, hi_v = min(values), max(values)
    if target:
        lo_v = min(lo_v, target[0])
        hi_v = max(hi_v, target[1])
    if hi_v == lo_v:  # flat series — pad so the line sits mid-canvas
        hi_v += 1.0
        lo_v -= 1.0
    span = hi_v - lo_v
    pad = span * 0.08
    lo_v -= pad
    hi_v += pad
    span = hi_v - lo_v

    n = len(pts)

    def x_at(i: int) -> float:
        if n == 1:
            return _PAD_L + _PLOT_W / 2
        return _PAD_L + _PLOT_W * i / (n - 1)

    def y_at(v: float) -> float:
        return _PAD_T + _PLOT_H * (1 - (v - lo_v) / span)

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {_W:g} {_H:g}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg" role="img">'
    ]

    # Target band.
    if target:
        y_hi, y_lo = y_at(target[1]), y_at(target[0])
        parts.append(
            f'<rect class="chart-band" x="{_PAD_L:g}" y="{y_hi:.1f}" '
            f'width="{_PLOT_W:g}" height="{max(0.0, y_lo - y_hi):.1f}"/>'
        )

    # Baseline + top frame.
    parts.append(
        f'<line class="chart-axis" x1="{_PAD_L:g}" y1="{_PAD_T + _PLOT_H:.1f}" '
        f'x2="{_W - _PAD_R:g}" y2="{_PAD_T + _PLOT_H:.1f}"/>'
    )

    # Area fill under the line.
    coords = [(x_at(i), y_at(p.value)) for i, p in enumerate(pts)]
    if n > 1:
        area = (
            f'M{coords[0][0]:.1f},{_PAD_T + _PLOT_H:.1f} '
            + " ".join(f'L{x:.1f},{y:.1f}' for x, y in coords)
            + f' L{coords[-1][0]:.1f},{_PAD_T + _PLOT_H:.1f} Z'
        )
        parts.append(f'<path class="chart-area" d="{area}"/>')
        line = "M" + " L".join(f'{x:.1f},{y:.1f}' for x, y in coords)
        parts.append(f'<path class="chart-line" d="{line}"/>')

    # Points (with hover tooltips) — last one emphasised.
    for i, (p, (x, y)) in enumerate(zip(pts, coords)):
        last = i == n - 1
        title = html.escape(
            f"{_fmt(p.value)}{unit} · {p.when.strftime('%d %b %H:%M')}"
        )
        parts.append(
            f'<circle class="chart-dot{" last" if last else ""}" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="{3.2 if last else 2.2:g}">'
            f'<title>{title}</title></circle>'
        )

    # Y range labels (max top-left, min bottom-left).
    parts.append(
        f'<text class="chart-yl" x="{_PAD_L:g}" y="{_PAD_T + 3:.0f}">{_fmt(hi_v)}</text>'
    )
    parts.append(
        f'<text class="chart-yl" x="{_PAD_L:g}" y="{_PAD_T + _PLOT_H - 1:.0f}">{_fmt(lo_v)}</text>'
    )

    # X date labels (first and last).
    parts.append(
        f'<text class="chart-xl" x="{_PAD_L:g}" y="{_H - 6:.0f}" '
        f'text-anchor="start">{pts[0].when.strftime("%d %b")}</text>'
    )
    if n > 1:
        parts.append(
            f'<text class="chart-xl" x="{_W - _PAD_R:g}" y="{_H - 6:.0f}" '
            f'text-anchor="end">{pts[-1].when.strftime("%d %b")}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
