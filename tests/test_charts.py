"""SVG line charts place points by time, not by index."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.charts import _PAD_L, _PLOT_W, Point, line_chart

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _dot_xs(svg: str) -> list[float]:
    return [float(x) for x in re.findall(r'<circle[^>]*cx="([\d.]+)"', svg)]


def test_points_are_spaced_by_time():
    # Gaps of 1 day then 9 days: the second gap should be nine times wider.
    pts = [
        Point(_T0, 7.2),
        Point(_T0 + timedelta(days=1), 7.4),
        Point(_T0 + timedelta(days=10), 7.6),
    ]
    xs = _dot_xs(line_chart(pts))
    assert xs[0] == _PAD_L and xs[-1] == _PAD_L + _PLOT_W
    assert abs((xs[2] - xs[1]) / (xs[1] - xs[0]) - 9) < 0.1


def test_explicit_span_positions_points_within_it():
    start, end = _T0, _T0 + timedelta(days=30)
    svg = line_chart([Point(_T0 + timedelta(days=15), 7.4)], start=start, end=end)
    (x,) = _dot_xs(svg)
    assert abs(x - (_PAD_L + _PLOT_W / 2)) < 0.2
    assert ">01 Jan<" in svg and ">31 Jan<" in svg


def test_single_instant_is_centred():
    (x,) = _dot_xs(line_chart([Point(_T0, 7.4)]))
    assert abs(x - (_PAD_L + _PLOT_W / 2)) < 0.2
