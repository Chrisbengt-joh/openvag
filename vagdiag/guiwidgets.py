"""Reusable tkinter widgets and styling for the VAGDIAG application.

Kept separate from :mod:`vagdiag.gui` so the application module stays about
screens and workflow rather than drawing code.
"""

from __future__ import annotations

import math
import tkinter as tk

__all__ = [
    "BACKGROUND",
    "PANEL",
    "TEXT",
    "MUTED",
    "ACTUAL_COLOUR",
    "SPEC_COLOUR",
    "ALERT",
    "OK_COLOUR",
    "SERIES_COLOURS",
    "CHART_WINDOW",
    "button",
    "heading_label",
    "body_label",
    "WarningPanel",
    "Gauge",
    "ScrollingChart",
]

# Colours - a dark panel is easier to read in a car, day or night.
BACKGROUND = "#12141a"
PANEL = "#1b1f2a"
RAISED = "#2a3142"
TEXT = "#e6e9f0"
MUTED = "#8b93a7"
ACTUAL_COLOUR = "#4da3ff"
SPEC_COLOUR = "#ffb454"
ALERT = "#ff5f56"
OK_COLOUR = "#3ddc84"

SERIES_COLOURS = [
    "#4da3ff", "#ffb454", "#3ddc84", "#ff5f56",
    "#c792ea", "#89ddff", "#f78c6c", "#a3be8c",
]

#: How many seconds the scrolling chart shows.
CHART_WINDOW = 60.0


# ---------------------------------------------------------------------------
# Small styled helpers
# ---------------------------------------------------------------------------


def button(
    master: tk.Misc,
    text: str,
    command,
    *,
    primary: bool = False,
    danger: bool = False,
    width: int | None = None,
) -> tk.Button:
    """A consistently styled button.

    ``primary`` marks the obvious next step, ``danger`` anything destructive.
    """
    colour = TEXT
    background = PANEL
    if primary:
        colour = BACKGROUND
        background = OK_COLOUR
    elif danger:
        colour = ALERT
    widget = tk.Button(
        master,
        text=text,
        command=command,
        bg=background,
        fg=colour,
        activebackground=RAISED,
        activeforeground=TEXT,
        relief="flat",
        padx=16,
        pady=7,
        font=("Segoe UI", 10, "bold" if primary else "normal"),
        cursor="hand2",
    )
    if width:
        widget.configure(width=width)
    return widget


def heading_label(master: tk.Misc, text: str, size: int = 13) -> tk.Label:
    """A section heading."""
    return tk.Label(
        master, text=text, bg=BACKGROUND, fg=TEXT,
        font=("Segoe UI", size, "bold"), anchor="w", justify="left",
    )


def body_label(master: tk.Misc, text: str, muted: bool = True,
               wraplength: int = 0) -> tk.Label:
    """A normal text label."""
    return tk.Label(
        master, text=text, bg=BACKGROUND, fg=MUTED if muted else TEXT,
        font=("Segoe UI", 9), anchor="w", justify="left",
        wraplength=wraplength,
    )


class WarningPanel(tk.Frame):
    """A boxed warning with a coloured bar down the left-hand side."""

    def __init__(self, master: tk.Misc, title: str, lines: list[str],
                 colour: str = ALERT) -> None:
        super().__init__(master, bg=PANEL)
        tk.Frame(self, bg=colour, width=5).pack(side="left", fill="y")
        inner = tk.Frame(self, bg=PANEL)
        inner.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        tk.Label(
            inner, text=title, bg=PANEL, fg=colour,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(fill="x")
        for line in lines:
            tk.Label(
                inner, text=line, bg=PANEL, fg=TEXT, font=("Segoe UI", 9),
                anchor="w", justify="left", wraplength=680,
            ).pack(fill="x", pady=(3, 0))


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


class Gauge(tk.Canvas):
    """Round gauge with a needle for the actual value and an optional spec needle."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        minimum: float,
        maximum: float,
        unit: str,
        size: int = 190,
    ) -> None:
        super().__init__(
            master, width=size, height=size, bg=PANEL, highlightthickness=0,
        )
        self.title = title
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit
        self.size = size
        self._draw_scale()
        self._needle_actual: int | None = None
        self._needle_spec: int | None = None
        self._text_actual = self.create_text(
            size / 2, size * 0.70, text="-",
            fill=TEXT, font=("Segoe UI", 15, "bold"),
        )
        self._text_spec = self.create_text(
            size / 2, size * 0.83, text="",
            fill=SPEC_COLOUR, font=("Segoe UI", 9),
        )

    def _draw_scale(self) -> None:
        s = self.size
        margin = s * 0.10
        self.create_arc(
            margin, margin, s - margin, s - margin,
            start=-45, extent=270, style=tk.ARC, outline="#2c3242", width=10,
        )
        for i in range(11):
            fraction = i / 10
            angle = math.radians(225 - 270 * fraction)
            outer = s / 2 - margin * 0.7
            inner = outer - (s * 0.045 if i % 5 == 0 else s * 0.025)
            centre = s / 2
            self.create_line(
                centre + inner * math.cos(angle), centre - inner * math.sin(angle),
                centre + outer * math.cos(angle), centre - outer * math.sin(angle),
                fill=MUTED, width=2 if i % 5 == 0 else 1,
            )
        self.create_text(
            s / 2, s * 0.20, text=self.title, fill=MUTED,
            font=("Segoe UI", 10, "bold"),
        )
        self.create_text(
            s / 2, s * 0.93, text=self.unit, fill=MUTED, font=("Segoe UI", 8),
        )

    def _needle_points(
        self, value: float, length: float
    ) -> tuple[float, float, float, float]:
        span = self.maximum - self.minimum or 1.0
        fraction = min(1.0, max(0.0, (value - self.minimum) / span))
        angle = math.radians(225 - 270 * fraction)
        centre = self.size / 2
        r = self.size / 2 * length
        return centre, centre, centre + r * math.cos(angle), centre - r * math.sin(angle)

    def update_values(self, actual: float | None, spec: float | None = None) -> None:
        """Redraw the needles. None hides the respective needle."""
        for handle in (self._needle_spec, self._needle_actual):
            if handle is not None:
                self.delete(handle)
        self._needle_spec = None
        self._needle_actual = None

        if spec is not None:
            self._needle_spec = self.create_line(
                *self._needle_points(spec, 0.66), fill=SPEC_COLOUR, width=3,
            )
            self.itemconfigure(
                self._text_spec, text=f"target {spec:,.0f}".replace(",", " ")
            )
        else:
            self.itemconfigure(self._text_spec, text="")

        if actual is not None:
            self._needle_actual = self.create_line(
                *self._needle_points(actual, 0.72), fill=ACTUAL_COLOUR, width=4,
            )
            self.itemconfigure(
                self._text_actual, text=f"{actual:,.0f}".replace(",", " ")
            )
        else:
            self.itemconfigure(self._text_actual, text="-")


# ---------------------------------------------------------------------------
# Scrolling chart
# ---------------------------------------------------------------------------


class ScrollingChart(tk.Canvas):
    """Real-time chart of the last ``CHART_WINDOW`` seconds.

    Each series is scaled against its own min/max within the window, because
    engine speed and charge pressure cannot share one axis. The current value
    is shown in the legend.
    """

    def __init__(self, master: tk.Misc, height: int = 220,
                 legend_width: int = 200) -> None:
        super().__init__(master, height=height, bg=PANEL, highlightthickness=0)
        self.legend_width = legend_width
        self._points: list[tuple[float, dict[str, tuple[float, str]]]] = []
        self.bind("<Configure>", lambda _e: self.redraw())

    def add(self, moment: float, series: dict[str, tuple[float, str]]) -> None:
        """Add one cycle: {label: (value, unit)}."""
        self._points.append((moment, series))
        cutoff = moment - CHART_WINDOW
        while self._points and self._points[0][0] < cutoff:
            self._points.pop(0)

    def clear(self) -> None:
        """Empty the chart."""
        self._points.clear()
        self.redraw()

    def redraw(self) -> None:
        """Repaint the whole chart."""
        self.delete("all")
        width = max(self.winfo_width(), 10)
        height = max(self.winfo_height(), 10)
        left, right, top, bottom = 8, width - self.legend_width, 12, height - 20

        for i in range(5):
            y = top + (bottom - top) * i / 4
            self.create_line(left, y, right, y, fill="#252b38")
        self.create_text(
            left + 4, bottom + 10, anchor="w",
            text=f"last {CHART_WINDOW:.0f} seconds", fill=MUTED,
            font=("Segoe UI", 8),
        )
        if len(self._points) < 2:
            self.create_text(
                (left + right) / 2, (top + bottom) / 2,
                text="collecting measurements ...", fill=MUTED,
                font=("Segoe UI", 10),
            )
            return

        end = self._points[-1][0]
        start = end - CHART_WINDOW
        names = list(self._points[-1][1].keys())

        for index, name in enumerate(names):
            colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
            values = [(t, s[name][0]) for t, s in self._points if name in s]
            if len(values) < 2:
                continue
            lowest = min(v for _t, v in values)
            highest = max(v for _t, v in values)
            span = (highest - lowest) or 1.0
            coordinates: list[float] = []
            for t, v in values:
                x = left + (right - left) * (t - start) / CHART_WINDOW
                y = bottom - (bottom - top) * (v - lowest) / span
                coordinates.extend((max(left, x), y))
            self.create_line(*coordinates, fill=colour, width=2, smooth=True)

            current, unit = self._points[-1][1][name]
            text_y = top + index * 16
            self.create_line(right + 8, text_y, right + 26, text_y,
                             fill=colour, width=3)
            self.create_text(
                right + 32, text_y, anchor="w",
                text=f"{name}  {current:.1f} {unit}",
                fill=TEXT, font=("Segoe UI", 8),
            )
