"""tkinter dashboard for VAGDIAG (phase 2).

Threading model
---------------
All serial communication happens in :class:`DiagnosticThread`. That thread
**never** touches tkinter. Results are put on a :class:`queue.Queue` which the
GUI thread drains from ``after()``. Commands going the other way (read faults,
clear faults, change groups, start/stop logging) travel through a second queue.
As a result the GUI can never freeze on a slow or dead K-line, and the serial
thread can never crash tkinter.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk
from typing import Sequence

from .datalog import CsvLogger
from .exceptions import VagdiagError
from .faults import FaultCode
from .formulas import Reading
from .kwp1281 import Identification
from .menu import Menu
from .modules import group_name, label, module_name

__all__ = ["run_gui", "Dashboard", "DiagnosticThread"]

# Colours - a dark panel is easier to read inside a car.
BACKGROUND = "#12141a"
PANEL = "#1b1f2a"
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
# Messages between the threads
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """One polling cycle."""

    time: float
    values: dict[int, list[Reading]] = field(default_factory=dict)


@dataclass
class StatusMessage:
    """A line for the status bar."""

    text: str
    error: bool = False


# ---------------------------------------------------------------------------
# The serial thread
# ---------------------------------------------------------------------------


class DiagnosticThread(threading.Thread):
    """Owns all ECU communication. Must never touch tkinter."""

    def __init__(
        self,
        menu: Menu,
        address: int,
        groups: Sequence[int],
        out: queue.Queue[object],
    ) -> None:
        super().__init__(name="vagdiag-serial", daemon=True)
        self.menu = menu
        self.address = address
        self.groups = list(groups)
        self.out = out
        self.incoming: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stop_event = threading.Event()
        self._logger: CsvLogger | None = None
        self._paused = False

    # -- commands from the GUI thread --------------------------------------

    def command(self, name: str, data: object = None) -> None:
        """Queue a command for the serial thread."""
        self.incoming.put((name, data))

    def stop(self) -> None:
        """Finish the thread and join it."""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=4.0)

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        client = None
        try:
            self.out.put(StatusMessage(
                f"Waking 0x{self.address:02X} {module_name(self.address)} ..."
            ))
            client = self.menu.new_client()
            ident = client.connect(self.address)
            client.start_keepalive()
            self.out.put(ident)
            self.out.put(StatusMessage(f"Connected: {ident.summary()}"))

            while not self._stop_event.is_set():
                self._handle_commands(client)
                if self._stop_event.is_set():
                    break
                if self._paused or not self.groups:
                    client.keep_alive()
                    time.sleep(0.2)
                    continue
                measurement = Measurement(time=time.monotonic())
                for group in self.groups:
                    measurement.values[group] = client.read_group(group)
                if self._logger is not None:
                    self._logger.log(measurement.values)
                self.out.put(measurement)
                if self.menu.interval:
                    time.sleep(self.menu.interval)
        except VagdiagError as error:
            self.out.put(error)
        except Exception as error:  # unexpected - still show something readable
            self.out.put(VagdiagError(f"Unexpected error in the serial thread: {error!r}"))
        finally:
            self._close_log()
            if client is not None:
                try:
                    client.disconnect()
                except VagdiagError:
                    pass
            self.out.put(StatusMessage("Disconnected."))

    def _handle_commands(self, client) -> None:
        """Drain the command queue."""
        while True:
            try:
                name, data = self.incoming.get_nowait()
            except queue.Empty:
                return
            if name == "groups":
                self.groups = list(data)  # type: ignore[arg-type]
                self._reset_log_groups()
            elif name == "pause":
                self._paused = bool(data)
            elif name == "read_faults":
                self.out.put(("faults", client.read_faults()))
            elif name == "clear_faults":
                client.clear_faults()
                self.out.put(StatusMessage("Fault memory cleared."))
                self.out.put(("faults", client.read_faults()))
            elif name == "log_start":
                self._start_log(str(data))
            elif name == "log_stop":
                self._close_log()
            elif name == "stop":
                self._stop_event.set()
                return

    # -- logging -----------------------------------------------------------

    def _start_log(self, filename: str) -> None:
        self._close_log()
        self._logger = CsvLogger(
            filename, self.groups, self.address,
            delimiter=self.menu.delimiter,
            decimal_comma=self.menu.decimal_comma,
        )
        self.out.put(StatusMessage(f"Logging to {filename}"))

    def _reset_log_groups(self) -> None:
        """The groups changed - a running log has to start a new file."""
        if self._logger is None:
            return
        old = self._logger.filename
        self._close_log()
        self.out.put(StatusMessage(
            f"Groups changed - the log {old.name} was closed. Start a new one."
        ))

    def _close_log(self) -> None:
        if self._logger is not None:
            name, rows = self._logger.filename, self._logger.rows
            self._logger.close()
            self._logger = None
            self.out.put(StatusMessage(f"Log saved: {name} ({rows} rows)"))

    @property
    def logging(self) -> bool:
        """True while logging is active."""
        return self._logger is not None


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

    # -- drawing -----------------------------------------------------------

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
                self._text_spec, text=f"SPEC {spec:,.0f}".replace(",", " ")
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

    def __init__(self, master: tk.Misc, height: int = 220) -> None:
        super().__init__(master, height=height, bg=PANEL, highlightthickness=0)
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
        left, right, top, bottom = 8, width - 150, 12, height - 20

        for i in range(5):
            y = top + (bottom - top) * i / 4
            self.create_line(left, y, right, y, fill="#252b38")
        self.create_text(
            left + 4, bottom + 10, anchor="w",
            text=f"last {CHART_WINDOW:.0f} s", fill=MUTED, font=("Segoe UI", 8),
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


# ---------------------------------------------------------------------------
# The main window
# ---------------------------------------------------------------------------


def _find_pair(
    measurement: Measurement, unit: str
) -> tuple[float | None, float | None]:
    """Find (SPEC, ACTUAL) for a unit, for example mbar or mg/stroke.

    VAG blocks put SPEC at position 2 and ACTUAL at position 3. When only one
    value carries the unit it is treated as the actual value.
    """
    for values in measurement.values.values():
        hits = [(i, v) for i, v in enumerate(values, start=1)
                if v.unit == unit and v.is_number]
        if len(hits) >= 2:
            return hits[0][1].number, hits[1][1].number
        if hits:
            return None, hits[0][1].number
    return None, None


def _find_single(measurement: Measurement, unit: str) -> float | None:
    """First numeric value carrying the given unit."""
    for values in measurement.values.values():
        for value in values:
            if value.unit == unit and value.is_number:
                return value.number
    return None


class Dashboard(tk.Tk):
    """The main window: gauges, real-time chart, logging and fault codes."""

    def __init__(self, menu: Menu, address: int = 0x01,
                 groups: Sequence[int] = (3, 11)) -> None:
        super().__init__()
        self.menu = menu
        self.address = address
        self.title("VAGDIAG - dashboard")
        self.geometry("1080x760")
        self.configure(bg=BACKGROUND)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: DiagnosticThread | None = None
        self._logging = False
        self._latest: Measurement | None = None

        self._build_ui(groups)
        self._start_thread(groups)
        self.after(50, self._drain_queue)

    # -- user interface ----------------------------------------------------

    def _build_ui(self, groups: Sequence[int]) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - platform dependent
            pass
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=TEXT,
                        padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", "#2a3142")])
        style.configure("TFrame", background=BACKGROUND)

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        measuring_tab = ttk.Frame(tabs)
        faults_tab = ttk.Frame(tabs)
        tabs.add(measuring_tab, text="  Measurements  ")
        tabs.add(faults_tab, text="  Fault codes  ")

        self._build_measuring_tab(measuring_tab, groups)
        self._build_faults_tab(faults_tab)

        self.status_bar = tk.Label(
            self, text="Starting ...", bg=BACKGROUND, fg=MUTED,
            anchor="w", font=("Segoe UI", 9),
        )
        self.status_bar.pack(fill="x", padx=12, pady=6)

    def _build_measuring_tab(self, frame: ttk.Frame, groups: Sequence[int]) -> None:
        gauge_row = tk.Frame(frame, bg=BACKGROUND)
        gauge_row.pack(fill="x", pady=(10, 4))

        self.gauge_rpm = Gauge(gauge_row, "ENGINE SPEED", 0, 5000, "rpm")
        self.gauge_boost = Gauge(gauge_row, "CHARGE PRESSURE", 800, 2400, "mbar")
        self.gauge_maf = Gauge(gauge_row, "AIR MASS", 0, 1000, "mg/stroke")
        for gauge in (self.gauge_rpm, self.gauge_boost, self.gauge_maf):
            gauge.pack(side="left", padx=10)

        values_frame = tk.Frame(gauge_row, bg=PANEL)
        values_frame.pack(side="left", fill="both", expand=True, padx=10)
        self.values_box = tk.Text(
            values_frame, bg=PANEL, fg=TEXT, bd=0, height=11,
            font=("Consolas", 9), state="disabled", wrap="none",
        )
        self.values_box.pack(fill="both", expand=True, padx=8, pady=8)

        self.chart = ScrollingChart(frame)
        self.chart.pack(fill="both", expand=True, padx=12, pady=8)

        controls = tk.Frame(frame, bg=BACKGROUND)
        controls.pack(fill="x", padx=12, pady=(0, 10))

        tk.Label(controls, text="Groups:", bg=BACKGROUND, fg=TEXT).pack(side="left")
        self.group_entry = tk.Entry(controls, width=18, bg=PANEL, fg=TEXT,
                                    insertbackground=TEXT, relief="flat")
        self.group_entry.insert(0, " ".join(str(g) for g in groups))
        self.group_entry.pack(side="left", padx=(6, 4))
        tk.Button(controls, text="Apply", command=self._change_groups,
                  bg=PANEL, fg=TEXT, relief="flat", padx=10).pack(side="left")

        self.log_button = tk.Button(
            controls, text="● Start logging", command=self._toggle_log,
            bg=PANEL, fg=OK_COLOUR, relief="flat", padx=14,
        )
        self.log_button.pack(side="right")
        self.pause_button = tk.Button(
            controls, text="Pause", command=self._toggle_pause,
            bg=PANEL, fg=TEXT, relief="flat", padx=14,
        )
        self.pause_button.pack(side="right", padx=8)

    def _build_faults_tab(self, frame: ttk.Frame) -> None:
        buttons = tk.Frame(frame, bg=BACKGROUND)
        buttons.pack(fill="x", padx=12, pady=10)
        tk.Button(buttons, text="Read fault codes", command=self._read_faults,
                  bg=PANEL, fg=TEXT, relief="flat", padx=14).pack(side="left")
        tk.Button(buttons, text="Clear fault codes", command=self._clear_faults,
                  bg=PANEL, fg=ALERT, relief="flat", padx=14).pack(side="left", padx=8)

        self.faults_box = tk.Text(
            frame, bg=PANEL, fg=TEXT, bd=0, font=("Consolas", 10),
            state="disabled", wrap="word",
        )
        self.faults_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._write_faults_box("Press \"Read fault codes\".")

    # -- thread plumbing ---------------------------------------------------

    def _start_thread(self, groups: Sequence[int]) -> None:
        self._thread = DiagnosticThread(self.menu, self.address, groups, self._queue)
        self._thread.start()

    def _drain_queue(self) -> None:
        """Collect messages from the serial thread. Runs in the GUI thread."""
        try:
            while True:
                self._handle(self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    def _handle(self, message: object) -> None:
        if isinstance(message, Measurement):
            self._show_measurement(message)
        elif isinstance(message, StatusMessage):
            self._status(message.text, message.error)
        elif isinstance(message, Identification):
            self.title(f"VAGDIAG - {message.name} {message.part_number}")
        elif isinstance(message, VagdiagError):
            self._show_error(message)
        elif isinstance(message, tuple) and message[0] == "faults":
            self._show_faults(message[1])

    def _status(self, text: str, error: bool = False) -> None:
        self.status_bar.configure(text=text, fg=ALERT if error else MUTED)

    def _show_error(self, error: VagdiagError) -> None:
        self._status(f"ERROR: {error.message}", error=True)
        messagebox.showerror(
            "Communication error",
            f"{error.message}\n\n{error.hint or ''}",
            parent=self,
        )

    # -- presentation ------------------------------------------------------

    def _show_measurement(self, measurement: Measurement) -> None:
        self._latest = measurement
        rpm = _find_single(measurement, "rpm")
        boost_spec, boost_actual = _find_pair(measurement, "mbar")
        maf_spec, maf_actual = _find_pair(measurement, "mg/stroke")

        self.gauge_rpm.update_values(rpm)
        self.gauge_boost.update_values(boost_actual, boost_spec)
        self.gauge_maf.update_values(maf_actual, maf_spec)

        series: dict[str, tuple[float, str]] = {}
        for group, values in measurement.values.items():
            for position, value in enumerate(values, start=1):
                if value.is_number and value.number is not None:
                    name = f"{group:03d}.{position} {label(group, position, self.address)}"
                    series[name[:34]] = (value.number, value.unit)
        self.chart.add(measurement.time, dict(list(series.items())[:8]))
        self.chart.redraw()
        self._write_values(measurement)

    def _write_values(self, measurement: Measurement) -> None:
        lines: list[str] = []
        for group, values in measurement.values.items():
            lines.append(f"Group {group:03d}  {group_name(group, self.address)}")
            if not values:
                lines.append("   (this module has no such group)")
            for position, value in enumerate(values, start=1):
                name = label(group, position, self.address)[:26]
                lines.append(f"   {position}  {name:<26} {value.text:>18}")
            lines.append("")
        self.values_box.configure(state="normal")
        self.values_box.delete("1.0", "end")
        self.values_box.insert("1.0", "\n".join(lines))
        self.values_box.configure(state="disabled")

    def _write_faults_box(self, text: str) -> None:
        self.faults_box.configure(state="normal")
        self.faults_box.delete("1.0", "end")
        self.faults_box.insert("1.0", text)
        self.faults_box.configure(state="disabled")

    def _show_faults(self, codes: Sequence[FaultCode]) -> None:
        self._status(f"{len(codes)} fault code(s) read.")
        if not codes:
            self._write_faults_box("No fault codes stored.")
            return
        lines = [f"{len(codes)} fault code(s):", ""]
        for code in codes:
            marker = "[INT]" if code.intermittent else "     "
            lines.append(f"{code.number} {marker} {code.text}")
            lines.append(f"             {code.status_text}")
            lines.append("")
        self._write_faults_box("\n".join(lines))

    # -- buttons -----------------------------------------------------------

    def _change_groups(self) -> None:
        groups: list[int] = []
        for chunk in self.group_entry.get().replace(",", " ").split():
            try:
                number = int(chunk)
            except ValueError:
                continue
            if 1 <= number <= 255:
                groups.append(number)
        if not groups:
            messagebox.showwarning(
                "Groups", "Enter at least one group between 1 and 255.", parent=self
            )
            return
        self.chart.clear()
        if self._thread:
            self._thread.command("groups", groups)
        self._status(f"Reading group {', '.join(f'{g:03d}' for g in groups)}")

    def _toggle_pause(self) -> None:
        if not self._thread:
            return
        pausing = self.pause_button.cget("text") == "Pause"
        self._thread.command("pause", pausing)
        self.pause_button.configure(text="Resume" if pausing else "Pause")

    def _toggle_log(self) -> None:
        if not self._thread:
            return
        if self._logging:
            self._thread.command("log_stop")
            self._logging = False
            self.log_button.configure(text="● Start logging", fg=OK_COLOUR)
            return
        filename = f"vagdiag_{datetime.now():%Y%m%d_%H%M%S}.csv"
        self._thread.command("log_start", filename)
        self._logging = True
        self.log_button.configure(text="■ Stop logging", fg=ALERT)

    def _read_faults(self) -> None:
        if self._thread:
            self._thread.command("read_faults")
            self._status("Reading fault codes ...")

    def _clear_faults(self) -> None:
        if not self._thread:
            return
        answer = simpledialog.askstring(
            "Clear fault codes",
            "The fault memory is erased permanently and cannot be recovered.\n"
            "Read and write down the codes first.\n\n"
            "Type YES to clear:",
            parent=self,
        )
        if answer != "YES":
            self._status("Clearing cancelled - nothing changed.")
            return
        self._thread.command("clear_faults")

    # -- shutdown ----------------------------------------------------------

    def _quit(self) -> None:
        if self._thread:
            self._thread.command("stop")
            self._thread.stop()
        self.destroy()


def run_gui(menu: Menu, address: int = 0x01,
            groups: Sequence[int] = (3, 11)) -> int:
    """Start the dashboard. Returns an exit code."""
    try:
        dashboard = Dashboard(menu, address, groups)
    except tk.TclError as error:  # pragma: no cover - needs a display
        print(f"Could not open a window: {error}")
        print("Use the terminal mode instead: python -m vagdiag <port>")
        return 2
    dashboard.mainloop()
    return 0
