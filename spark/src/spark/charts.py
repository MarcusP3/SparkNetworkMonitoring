"""Charts as server-rendered SVG. No chart library, no build step, no CDN.

SPARK runs on LANs without internet, under a CSP that allows only its own
scripts and no inline styles. A JavaScript chart library would be a vendored
dependency larger than the rest of the front end put together; the charts here
need lines, a grid and labels, which is a few dozen lines of SVG.

How it stays legible at every width:

  * The plot is an SVG stretched to its box (`preserveAspectRatio="none"`),
    with `vector-effect: non-scaling-stroke` so lines keep their thickness
    however it is stretched.
  * The text is HTML, not SVG, so it never stretches. Gridlines sit at fixed
    quarters, and the scale's maximum is rounded so that each quarter is a
    readable number -- which is what lets the labels be positioned by class
    rather than by an inline style the CSP would refuse.

Times are rendered in the time zone chosen under Preferences, and
`static/charts.js` re-formats them in the browser's own clock style (12- or
24-hour) in that same zone, and draws the hover readout. With scripts off the
charts still render, in 24-hour time, with no hover readout.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from markupsafe import Markup, escape

from .snmp_history import Window

PLOT_W = 1000.0
PLOT_H = 100.0

# 1, 2, 4, 6, 8 times a power of ten: each divides into quarters that are
# themselves readable (8 -> 2, 4, 6), which is what the gridlines are.
_NICE = (1.0, 2.0, 4.0, 6.0, 8.0, 10.0)


def nice_max(value: float | None, *, floor: float = 1.0) -> float:
    """The smallest readable scale maximum at or above `value`."""
    value = max(value or 0.0, floor)
    exponent = math.floor(math.log10(value))
    base = 10 ** exponent
    for step in _NICE:
        if step * base >= value * (1 - 1e-9):
            return step * base
    return 10 * base  # pragma: no cover - _NICE ends at 10


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def fmt_bps(value: float | None) -> str:
    """Bits per second in decimal units, as network speeds are quoted."""
    if value is None:
        return "—"
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if value >= size:
            scaled = value / size
            return f"{scaled:.3g} {unit}" if scaled < 100 else f"{scaled:.0f} {unit}"
    return f"{value:.0f} bps"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def fmt_temp(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}°C"


def fmt_load(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def _x(index: int, count: int) -> float:
    return (index + 0.5) * PLOT_W / count


def _y(value: float, y_max: float) -> float:
    return PLOT_H - max(0.0, min(value / y_max, 1.0)) * PLOT_H


def _segments(values: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    """Runs of consecutive values. A None is a gap, and gaps are not bridged."""
    runs: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append((index, value))
    if current:
        runs.append(current)
    return runs


def line_path(values: Sequence[float | None], y_max: float) -> str:
    """An SVG path through the values, broken wherever one is missing.

    A lone point between two gaps becomes a short dash rather than nothing:
    one answered poll in an hour of silence should still be visible.
    """
    count = len(values)
    half = PLOT_W / count / 2
    parts = []
    for run in _segments(values):
        if len(run) == 1:
            index, value = run[0]
            x, y = _x(index, count), _y(value, y_max)
            parts.append(f"M{x - half:.1f},{y:.1f}H{x + half:.1f}")
            continue
        points = "L".join(f"{_x(i, count):.1f},{_y(v, y_max):.1f}" for i, v in run)
        parts.append("M" + points)
    return "".join(parts)


def area_path(values: Sequence[float | None], y_max: float) -> str:
    """The same runs, closed down to the baseline, for a light fill under a line."""
    count = len(values)
    half = PLOT_W / count / 2
    parts = []
    for run in _segments(values):
        first, last = run[0][0], run[-1][0]
        left = _x(first, count) - (half if len(run) == 1 else 0)
        right = _x(last, count) + (half if len(run) == 1 else 0)
        points = "L".join(f"{_x(i, count):.1f},{_y(v, y_max):.1f}" for i, v in run)
        if len(run) == 1:
            y = _y(run[0][1], y_max)
            points = f"{left:.1f},{y:.1f}L{right:.1f},{y:.1f}"
        parts.append(f"M{left:.1f},{PLOT_H}L{points}L{right:.1f},{PLOT_H}Z")
    return "".join(parts)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


@dataclass
class Line:
    """One series on a chart. `kind` picks its colour from the stylesheet."""

    avg: Sequence[float | None]
    peak: Sequence[float | None] | None = None
    kind: str = "primary"      # "primary" (brand) or "secondary" (neutral)
    label: str = ""
    fill: bool = False


def _time_label(when: datetime, style: str, tz=timezone.utc) -> Markup:  # type: ignore[no-untyped-def]
    local = when.astimezone(tz)
    text = local.strftime("%H:%M") if style == "time" else local.strftime("%b %-d")
    return Markup(
        f'<time datetime="{when.isoformat()}" data-style="{style}">{escape(text)}</time>'
    )


def x_labels(window: Window, tz=timezone.utc) -> list[Markup]:  # type: ignore[no-untyped-def]
    """Five times across the window: both ends and the quarters between."""
    span = window.width * window.count
    style = "time" if span <= 86400 else "date"
    return [
        _time_label(window.start + timedelta(seconds=span * f), style, tz)
        for f in (0, 0.25, 0.5, 0.75, 1)
    ]


def chart(
    lines: Sequence[Line],
    window: Window,
    *,
    y_max: float,
    fmt: Callable[[float | None], str],
    title: str,
    describe: Callable[[int], str] | None = None,
    tz=timezone.utc,  # type: ignore[no-untyped-def]
    tz_name: str = "UTC",
) -> Markup:
    """A complete chart: plot, grid, value axis, time axis, hover readouts.

    `describe(i)` gives the readout for bucket i, shown as a tooltip when the
    pointer is over that part of the plot.
    """
    count = window.count
    grid = "".join(
        f'<line class="grid" x1="0" x2="{PLOT_W:.0f}" y1="{PLOT_H * f:.0f}" '
        f'y2="{PLOT_H * f:.0f}" vector-effect="non-scaling-stroke"/>'
        for f in (0, 0.25, 0.5, 0.75, 1)
    )
    body = []
    for line in lines:
        if line.fill:
            body.append(f'<path class="area {line.kind}" d="{area_path(line.avg, y_max)}"/>')
        if line.peak is not None:
            body.append(
                f'<path class="line peak {line.kind}" d="{line_path(line.peak, y_max)}" '
                'vector-effect="non-scaling-stroke"/>'
            )
        body.append(
            f'<path class="line {line.kind}" d="{line_path(line.avg, y_max)}" '
            'vector-effect="non-scaling-stroke"/>'
        )

    # Readouts for the hover line, one string per bucket, as a JSON attribute
    # read by static/charts.js. A <title> per bucket did the same without
    # script, and made a 24-hour page 300 KB of tooltips.
    readouts = json.dumps(
        [describe(i) or "" for i in range(count)] if describe else [],
        ensure_ascii=False, separators=(",", ":"),
    )

    y_axis = "".join(
        f'<span class="tick q{q}">{escape(fmt(y_max * q / 4))}</span>'
        for q in (4, 3, 2, 1, 0)
    )
    x_axis = "".join(f"<span>{label}</span>" for label in x_labels(window, tz))
    return Markup(
        f'<figure class="chart" data-start="{window.start_epoch}" data-tz="{escape(tz_name)}" '
        f"data-width=\"{window.width}\" data-readouts='{_attr(readouts)}'>"
        f'<div class="chart-body"><div class="chart-y" aria-hidden="true">{y_axis}</div>'
        '<div class="chart-plot">'
        f'<svg viewBox="0 0 {PLOT_W:.0f} {PLOT_H:.0f}" preserveAspectRatio="none" '
        f'role="img" aria-label="{escape(title)}">{grid}{"".join(body)}'
        '<line class="cursor" x1="0" x2="0" y1="0" y2="100" '
        'vector-effect="non-scaling-stroke"/></svg>'
        '<div class="chart-readout" hidden></div>'
        '</div></div>'
        f'<div class="chart-x" aria-hidden="true">{x_axis}</div>'
        '</figure>'
    )


def _attr(value: str) -> str:
    """Escape for a single-quoted attribute.

    JSON is full of double quotes; in a double-quoted attribute each becomes
    six bytes of `&#34;`. Single-quoted, only the rare apostrophe needs it.
    """
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("'", "&#39;"))


SPARK_POINTS = 72


def _thin(values: Sequence[float | None], points: int) -> list[float | None]:
    """Average consecutive values down to at most `points`, keeping gaps.

    A sparkline is 8rem wide. 288 points in it is ten per pixel, and on a
    48-port switch that was a third of a megabyte of path data for detail
    nobody can see.
    """
    if len(values) <= points:
        return list(values)
    size = math.ceil(len(values) / points)
    out: list[float | None] = []
    for i in range(0, len(values), size):
        group = [v for v in values[i:i + size] if v is not None]
        out.append(sum(group) / len(group) if group else None)
    return out


def sparkline(inbound: Sequence[float | None], outbound: Sequence[float | None]) -> Markup:
    """A small in/out trace for a table row, scaled to its own peak.

    Its own peak, not a shared scale: the question a row answers is "when was
    this port busy", and on a shared scale every port but the uplink is a
    flat line.
    """
    top = max([v for v in list(inbound) + list(outbound) if v is not None], default=0.0)
    if top <= 0:
        return Markup('<span class="muted small">—</span>')
    y_max = top * 1.05
    inbound, outbound = _thin(inbound, SPARK_POINTS), _thin(outbound, SPARK_POINTS)
    return Markup(
        f'<svg class="sparkline" viewBox="0 0 {PLOT_W:.0f} {PLOT_H:.0f}" '
        'preserveAspectRatio="none" aria-hidden="true">'
        f'<path class="line secondary" d="{line_path(outbound, y_max)}" '
        'vector-effect="non-scaling-stroke"/>'
        f'<path class="line primary" d="{line_path(inbound, y_max)}" '
        'vector-effect="non-scaling-stroke"/>'
        '</svg>'
    )
