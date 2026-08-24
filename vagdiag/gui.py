"""The VAGDIAG desktop application.

A complete graphical front end, aimed at people who would rather not touch a
terminal. It opens on a connection screen (pick the cable, pick the module, or
practise against the built-in simulator) and then presents everything the
terminal menu can do as tabs: measurements, fault codes, the actuator test,
adaptation and basic settings.

Threading: every byte of ECU traffic happens on a worker thread from
:mod:`vagdiag.guiworker`. Those threads never touch tkinter; they post messages
onto a queue that this module drains from ``after()``. The window therefore
cannot freeze, no matter how badly the K-line misbehaves.
"""

from __future__ import annotations

import queue
import sys
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Sequence

from .exceptions import VagdiagError
from .faults import FaultCode
from .guiwidgets import (
    ACTUAL_COLOUR,
    ALERT,
    BACKGROUND,
    MUTED,
    OK_COLOUR,
    PANEL,
    RAISED,
    SPEC_COLOUR,
    TEXT,
    Gauge,
    ScrollingChart,
    WarningPanel,
    body_label,
    button,
    heading_label,
)
from .guiworker import (
    DiagnosticWorker,
    Measurement,
    Result,
    ScanWorker,
    Settings,
    StatusMessage,
)
from .kwp1281 import Actuator, Adaptation, Identification, ScanResult
from .modules import (
    ADDRESSES,
    AUTOSCAN_ADDRESSES,
    SENSITIVE_ADDRESSES,
    group_name,
    label,
    module_name,
)
from .transport import (
    SerialTransport,
    list_ports,
    pyserial_available,
    read_ftdi_latency,
    set_ftdi_latency,
)

__all__ = ["run_gui", "App"]

#: Groups offered as tick boxes on the measurement tab.
QUICK_GROUPS = (1, 2, 3, 4, 11, 13)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monospace_box(master: tk.Misc, height: int = 12,
                   wrap: str = "word") -> tk.Text:
    """A read-only monospace text area.

    Tabular output passes ``wrap="none"`` so aligned columns stay aligned.
    """
    return tk.Text(
        master, bg=PANEL, fg=TEXT, bd=0, height=height,
        font=("Consolas", 10), state="disabled", wrap=wrap,
        padx=10, pady=8,
    )


def _set_text(widget: tk.Text, text: str) -> None:
    """Replace the contents of a read-only text area."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


def _confirm_word(parent: tk.Misc, title: str, message: str, word: str) -> bool:
    """Ask the user to type an exact word before something destructive happens."""
    answer = simpledialog.askstring(
        title, f"{message}\n\nType {word} to continue:", parent=parent
    )
    return answer == word


def _find_pair(
    measurement: Measurement, unit: str
) -> tuple[float | None, float | None]:
    """Find (target, actual) for a unit, for example mbar or mg/stroke.

    VAG blocks put the target at position 2 and the actual value at position 3.
    A lone value carrying the unit is treated as the actual one.
    """
    for values in measurement.values.values():
        hits = [v for v in values if v.unit == unit and v.is_number]
        if len(hits) >= 2:
            return hits[0].number, hits[1].number
        if hits:
            return None, hits[0].number
    return None, None


def _find_single(measurement: Measurement, unit: str) -> float | None:
    """First numeric value carrying the given unit."""
    for values in measurement.values.values():
        for value in values:
            if value.unit == unit and value.is_number:
                return value.number
    return None


def _enable_dpi_awareness() -> None:
    """Ask Windows for real pixels.

    Without this the window is bitmap-stretched on a display running at 125% or
    150%, which makes every label look blurry. Must run before the Tk root is
    created.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # per-monitor aware
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Connection screen
# ---------------------------------------------------------------------------


class ConnectScreen(tk.Frame):
    """Pick a cable or the simulator, then a control module."""

    def __init__(self, app: App) -> None:
        super().__init__(app.container, bg=BACKGROUND)
        self.app = app
        self.scanning = False

        wrapper = tk.Frame(self, bg=BACKGROUND)
        wrapper.place(relx=0.5, rely=0.5, anchor="center")

        heading_label(wrapper, "VAGDIAG", 22).pack(anchor="w")
        body_label(
            wrapper,
            "Diagnostics for older VAG cars that use KWP1281 over the K-line\n"
            "(roughly 1996-2003, before CAN). Hobby tool - use at your own risk.",
        ).pack(anchor="w", pady=(2, 18))

        # -- where to connect ----------------------------------------------
        self.source = tk.StringVar(value="simulator" if app.use_simulator else "cable")
        source_frame = tk.Frame(wrapper, bg=BACKGROUND)
        source_frame.pack(fill="x", pady=(0, 10))
        for value, text in (
            ("cable", "KKL cable (a real car)"),
            ("simulator", "Simulator (practise without a car)"),
        ):
            tk.Radiobutton(
                source_frame, text=text, value=value, variable=self.source,
                command=self._source_changed, bg=BACKGROUND, fg=TEXT,
                selectcolor=PANEL, activebackground=BACKGROUND,
                activeforeground=TEXT, font=("Segoe UI", 10),
                highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 18))

        # -- port -----------------------------------------------------------
        self.port_frame = tk.Frame(wrapper, bg=BACKGROUND)
        self.port_frame.pack(fill="x", pady=(0, 4))
        tk.Label(self.port_frame, text="Port:", bg=BACKGROUND, fg=TEXT,
                 font=("Segoe UI", 10), width=8, anchor="w").pack(side="left")
        self.port_box = ttk.Combobox(self.port_frame, width=34, state="readonly")
        self.port_box.pack(side="left", padx=(0, 8))
        self.port_box.bind("<<ComboboxSelected>>", lambda _e: self._show_latency())
        button(self.port_frame, "Refresh", self.refresh_ports).pack(side="left")

        self.latency_label = body_label(wrapper, "", wraplength=560)
        self.latency_label.pack(anchor="w", padx=(64, 0))
        self.latency_button = button(
            wrapper, "Set latency timer to 1 ms", self._fix_latency
        )

        # -- module ---------------------------------------------------------
        module_frame = tk.Frame(wrapper, bg=BACKGROUND)
        module_frame.pack(fill="x", pady=(14, 4))
        tk.Label(module_frame, text="Module:", bg=BACKGROUND, fg=TEXT,
                 font=("Segoe UI", 10), width=8, anchor="w").pack(side="left")
        self.module_box = ttk.Combobox(module_frame, width=34, state="readonly")
        self.module_box["values"] = [
            f"{a:02X}  {ADDRESSES.get(a, '')}" for a in AUTOSCAN_ADDRESSES
        ]
        self.module_box.current(0)
        self.module_box.pack(side="left")

        # -- actions --------------------------------------------------------
        actions = tk.Frame(wrapper, bg=BACKGROUND)
        actions.pack(fill="x", pady=(20, 0))
        self.connect_button = button(
            actions, "Connect", self._connect, primary=True, width=14
        )
        self.connect_button.pack(side="left")
        self.scan_button = button(actions, "Scan every module", self._scan)
        self.scan_button.pack(side="left", padx=8)
        button(actions, "Quit", self.app.quit_app).pack(side="right")

        # -- scan output ----------------------------------------------------
        self.scan_box = _monospace_box(wrapper, height=11)
        self.status = body_label(wrapper, "", wraplength=620)
        self.status.pack(anchor="w", pady=(14, 0))

        self.refresh_ports()
        self._source_changed()

    # -- ports -------------------------------------------------------------

    def refresh_ports(self) -> None:
        """Re-read the list of serial ports."""
        if not pyserial_available():
            self.port_box["values"] = []
            self.port_box.set("pyserial is not installed")
            self.latency_label.configure(
                text="pyserial is missing, so no cable can be opened. Install it "
                     "with:  pip install pyserial\nYou can still use the "
                     "simulator.",
                fg=ALERT,
            )
            return
        ports = list_ports()
        self.port_box["values"] = [f"{name}  -  {desc}" for name, desc in ports]
        if ports:
            preset = self.app.preset_port
            index = 0
            if preset:
                for i, (name, _d) in enumerate(ports):
                    if name.upper() == preset.upper():
                        index = i
                        break
            self.port_box.current(index)
        else:
            self.port_box.set("")
        self._show_latency()

    def selected_port(self) -> str | None:
        """The port name currently chosen, or None."""
        text = self.port_box.get()
        if not text or "not installed" in text:
            return None
        return text.split("  -  ")[0].strip()

    def _show_latency(self) -> None:
        """Report the FTDI latency timer for the selected port."""
        self.latency_button.pack_forget()
        port = self.selected_port()
        if not port:
            self.latency_label.configure(
                text="No serial port found. Plug in the KKL cable and press "
                     "Refresh.", fg=MUTED,
            )
            return
        latency = read_ftdi_latency(port)
        if latency is None:
            self.latency_label.configure(
                text="Latency timer: unknown. It must be 1 ms for KWP1281 to "
                     "work reliably - see the README if connecting fails.",
                fg=MUTED,
            )
            return
        if latency <= 2:
            self.latency_label.configure(
                text=f"Latency timer: {latency} ms - good.", fg=OK_COLOUR
            )
            return
        self.latency_label.configure(
            text=f"Latency timer: {latency} ms - too slow. KWP1281 acknowledges "
                 "every byte, so anything above 2 ms drops the connection.",
            fg=ALERT,
        )
        self.latency_button.pack(anchor="w", padx=(64, 0), pady=(6, 0))

    def _fix_latency(self) -> None:
        """Try to write the latency timer, explaining what to do if it fails."""
        port = self.selected_port()
        if not port:
            return
        if set_ftdi_latency(port, 1):
            messagebox.showinfo(
                "Latency timer",
                f"The latency timer for {port} is now 1 ms.\n\n"
                "Unplug the USB cable and plug it back in for the change to "
                "take effect, then press Refresh.",
                parent=self,
            )
        else:
            messagebox.showwarning(
                "Latency timer",
                "Could not change the setting - Windows requires administrator "
                "rights for this.\n\nEither restart VAGDIAG as administrator, "
                "or set it by hand:\n\n"
                "Device Manager -> Ports (COM & LPT) -> USB Serial Port -> "
                "Properties -> Port Settings -> Advanced -> "
                "Latency Timer (msec) = 1",
                parent=self,
            )
        self._show_latency()

    # -- state -------------------------------------------------------------

    def _source_changed(self) -> None:
        """Show or hide the port controls depending on cable vs simulator."""
        simulator = self.source.get() == "simulator"
        state = "disabled" if simulator else "readonly"
        self.port_box.configure(state=state)
        if simulator:
            self.latency_button.pack_forget()
            self.latency_label.configure(
                text="The simulator answers exactly like a real engine module, "
                     "so you can learn the program safely before going near the "
                     "car.", fg=MUTED,
            )
        else:
            self._show_latency()

    def selected_address(self) -> int:
        """The module address currently chosen."""
        return int(self.module_box.get().split()[0], 16)

    def set_busy(self, busy: bool, text: str = "") -> None:
        """Disable the buttons while something is running."""
        state = "disabled" if busy else "normal"
        self.connect_button.configure(state=state)
        self.scan_button.configure(state=state)
        self.status.configure(text=text, fg=MUTED)

    # -- actions -----------------------------------------------------------

    def _connect(self) -> None:
        simulator = self.source.get() == "simulator"
        port = None if simulator else self.selected_port()
        if not simulator and not port:
            messagebox.showwarning(
                "No port",
                "Pick a serial port first, or choose the simulator to practise "
                "without a car.",
                parent=self,
            )
            return
        self.set_busy(True, "Connecting ...")
        self.app.connect(simulator, port, self.selected_address())

    def _scan(self) -> None:
        simulator = self.source.get() == "simulator"
        port = None if simulator else self.selected_port()
        if not simulator and not port:
            messagebox.showwarning(
                "No port", "Pick a serial port first.", parent=self
            )
            return
        self.scan_box.pack(fill="both", expand=True, pady=(14, 0))
        _set_text(self.scan_box, "Scanning. Most addresses stay silent - that is "
                                 "normal in a car from 1999.\n\n")
        self.set_busy(True, "Scanning every module ...")
        self.scanning = True
        self.app.start_scan(simulator, port)

    # -- messages from the workers -----------------------------------------

    def handle(self, message: object) -> None:
        """Handle one message from a worker thread."""
        if isinstance(message, StatusMessage):
            self.status.configure(
                text=message.text, fg=ALERT if message.error else MUTED
            )
        elif isinstance(message, Result) and message.kind == "scan_progress":
            self._append_scan(message.payload)  # type: ignore[arg-type]
        elif isinstance(message, Result) and message.kind == "scan_done":
            self._finish_scan(message.payload)  # type: ignore[arg-type]

    def _append_scan(self, entry: ScanResult) -> None:
        if entry.responded:
            line = (f"0x{entry.address:02X}  {entry.name:<26} responds, "
                    f"{len(entry.faults)} fault code(s)\n")
        else:
            line = f"0x{entry.address:02X}  {entry.name:<26} silent\n"
        self.scan_box.configure(state="normal")
        self.scan_box.insert("end", line)
        self.scan_box.see("end")
        self.scan_box.configure(state="disabled")

    def _finish_scan(self, results: list[ScanResult]) -> None:
        self.scanning = False
        self.set_busy(False)
        found = [r for r in results if r.responded]
        total = sum(len(r.faults) for r in found)
        lines = [
            "",
            "-" * 64,
            f"Finished {datetime.now():%Y-%m-%d %H:%M}: {len(found)} of "
            f"{len(results)} addresses responded, {total} fault code(s).",
            "",
        ]
        for entry in found:
            assert entry.ident is not None
            lines.append(f"0x{entry.address:02X}  {entry.name}")
            lines.append(f"      {entry.ident.part_number} {entry.ident.component}")
            for code in entry.faults:
                marker = "intermittent" if code.intermittent else "static"
                lines.append(f"      {code.number}  {code.text}  ({marker})")
            if not entry.faults:
                lines.append("      no fault codes")
            lines.append("")
        if not found:
            lines.append("Nothing answered. Check the ignition, the cable and the")
            lines.append("latency timer, then try again.")
        self.scan_box.configure(state="normal")
        self.scan_box.insert("end", "\n".join(lines))
        self.scan_box.see("end")
        self.scan_box.configure(state="disabled")
        self.status.configure(
            text="Scan finished. Pick a module above and press Connect.",
            fg=OK_COLOUR,
        )


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


class MainScreen(tk.Frame):
    """Tabs for everything you can do with a connected control module."""

    def __init__(self, app: App, ident: Identification) -> None:
        super().__init__(app.container, bg=BACKGROUND)
        self.app = app
        self.ident = ident
        self.address = ident.address
        self.logging = False
        self.polling = True
        self.actuator_running = False
        self.adaptation_channel: int | None = None
        self.adaptation_old: int | None = None
        self.adaptation_tested: int | None = None

        self._build_header()
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self._build_measurement_tab()
        self._build_faults_tab()
        self._build_actuator_tab()
        self._build_adaptation_tab()
        self._build_basic_tab()
        self._build_info_tab()
        self._build_status_bar()

    # -- chrome ------------------------------------------------------------

    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=BACKGROUND)
        bar.pack(fill="x", padx=14, pady=(12, 0))
        heading_label(bar, f"{self.ident.name}  (0x{self.address:02X})", 14).pack(
            side="left"
        )
        detail = " ".join(
            part for part in (self.ident.part_number, self.ident.component) if part
        )
        body_label(bar, f"    {detail}").pack(side="left")
        button(bar, "Disconnect", self.app.disconnect).pack(side="right")

    def _build_status_bar(self) -> None:
        self.status = tk.Label(
            self, text="Connected.", bg=BACKGROUND, fg=MUTED,
            anchor="w", font=("Segoe UI", 9),
        )
        self.status.pack(fill="x", padx=14, pady=8)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, fg=ALERT if error else MUTED)

    def _new_tab(self, title: str) -> tk.Frame:
        frame = tk.Frame(self.tabs, bg=BACKGROUND)
        self.tabs.add(frame, text=f"  {title}  ")
        return frame

    # -- measurements ------------------------------------------------------

    def _build_measurement_tab(self) -> None:
        tab = self._new_tab("Measurements")

        picker = tk.Frame(tab, bg=BACKGROUND)
        picker.pack(fill="x", padx=10, pady=(10, 4))
        body_label(picker, "Show:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.group_vars: dict[int, tk.BooleanVar] = {}
        # Three per row, so the labels never run off the edge of the window.
        for index, group in enumerate(QUICK_GROUPS):
            variable = tk.BooleanVar(value=group in self.app.groups)
            self.group_vars[group] = variable
            tk.Checkbutton(
                picker, text=f"{group:03d} {group_name(group, self.address)}",
                variable=variable, command=self._apply_groups,
                bg=BACKGROUND, fg=TEXT, selectcolor=PANEL,
                activebackground=BACKGROUND, activeforeground=TEXT,
                font=("Segoe UI", 9), highlightthickness=0, bd=0, anchor="w",
            ).grid(row=index // 3, column=1 + index % 3, sticky="w", padx=(0, 14))

        custom = tk.Frame(tab, bg=BACKGROUND)
        custom.pack(fill="x", padx=10, pady=(0, 8))
        body_label(custom, "Other groups (numbers, space separated):").pack(side="left")
        self.custom_entry = tk.Entry(
            custom, width=16, bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat",
        )
        self.custom_entry.pack(side="left", padx=8)
        button(custom, "Add", self._add_custom_groups).pack(side="left")

        gauges = tk.Frame(tab, bg=BACKGROUND)
        gauges.pack(fill="x", padx=10)
        size = self.app.px(170)
        self.gauge_rpm = Gauge(gauges, "ENGINE SPEED", 0, 5000, "rpm", size)
        self.gauge_boost = Gauge(gauges, "CHARGE PRESSURE", 800, 2400, "mbar", size)
        self.gauge_air = Gauge(gauges, "AIR MASS", 0, 1000, "mg/stroke", size)
        for gauge in (self.gauge_rpm, self.gauge_boost, self.gauge_air):
            gauge.pack(side="left", padx=(0, 10))
        self.values_box = _monospace_box(gauges, height=11, wrap="none")
        self.values_box.pack(side="left", fill="both", expand=True)

        self.chart = ScrollingChart(
            tab, height=self.app.px(200), legend_width=self.app.px(210)
        )
        self.chart.pack(fill="both", expand=True, padx=10, pady=8)

        controls = tk.Frame(tab, bg=BACKGROUND)
        controls.pack(fill="x", padx=10, pady=(0, 10))
        self.pause_button = button(controls, "Pause", self._toggle_pause)
        self.pause_button.pack(side="left")
        self.log_button = button(
            controls, "Start logging to a file", self._toggle_log, primary=True
        )
        self.log_button.pack(side="left", padx=8)
        body_label(
            controls,
            "Values marked ? use reconstructed formulas - check them against VCDS.",
        ).pack(side="right")

    def _selected_groups(self) -> list[int]:
        return sorted(g for g, v in self.group_vars.items() if v.get())

    def _apply_groups(self) -> None:
        groups = self._selected_groups()
        if not groups:
            self._set_status("Tick at least one group to see measurements.")
            self.app.send("groups", [])
            return
        self.app.groups = groups
        self.chart.clear()
        self.app.send("groups", groups)
        names = ", ".join(f"{g:03d}" for g in groups)
        self._set_status(f"Reading group {names}.")

    def _add_custom_groups(self) -> None:
        added = []
        for chunk in self.custom_entry.get().replace(",", " ").split():
            try:
                number = int(chunk)
            except ValueError:
                continue
            if 1 <= number <= 255 and number not in self.group_vars:
                variable = tk.BooleanVar(value=True)
                self.group_vars[number] = variable
                added.append(number)
            elif number in self.group_vars:
                self.group_vars[number].set(True)
                added.append(number)
        self.custom_entry.delete(0, "end")
        if added:
            self._apply_groups()
        else:
            self._set_status("Enter group numbers between 1 and 255.")

    def _toggle_pause(self) -> None:
        self.polling = not self.polling
        self.app.send("polling", self.polling)
        self.pause_button.configure(text="Pause" if self.polling else "Resume")
        self._set_status("Reading." if self.polling else "Paused.")

    def _toggle_log(self) -> None:
        if self.logging:
            self.app.send("log_stop")
            self.logging = False
            self.log_button.configure(text="Start logging to a file", fg=BACKGROUND)
            return
        filename = filedialog.asksaveasfilename(
            parent=self,
            title="Save the measurement log",
            defaultextension=".csv",
            initialfile=f"vagdiag_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV spreadsheet", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        self.app.send("log_start", filename)
        self.logging = True
        self.log_button.configure(text="Stop logging", fg=ALERT)

    def _show_measurement(self, measurement: Measurement) -> None:
        rpm = _find_single(measurement, "rpm")
        boost_spec, boost_actual = _find_pair(measurement, "mbar")
        air_spec, air_actual = _find_pair(measurement, "mg/stroke")
        self.gauge_rpm.update_values(rpm)
        self.gauge_boost.update_values(boost_actual, boost_spec)
        self.gauge_air.update_values(air_actual, air_spec)

        series: dict[str, tuple[float, str]] = {}
        lines: list[str] = []
        for group, values in measurement.values.items():
            lines.append(f"Group {group:03d}  {group_name(group, self.address)}")
            if not values:
                lines.append("   (this module has no such group)")
            for position, value in enumerate(values, start=1):
                name = label(group, position, self.address)
                lines.append(f"  {name[:23]:<23} {value.text:>15}")
                if value.is_number and value.number is not None:
                    key = f"{group:03d}.{position} {name}"[:24]
                    series[key] = (value.number, value.unit)
            lines.append("")
        _set_text(self.values_box, "\n".join(lines))
        self.chart.add(measurement.time, dict(list(series.items())[:8]))
        self.chart.redraw()

    # -- fault codes -------------------------------------------------------

    def _build_faults_tab(self) -> None:
        tab = self._new_tab("Fault codes")
        bar = tk.Frame(tab, bg=BACKGROUND)
        bar.pack(fill="x", padx=10, pady=10)
        button(bar, "Read fault codes", self._read_faults, primary=True).pack(
            side="left"
        )
        button(bar, "Clear fault codes", self._clear_faults, danger=True).pack(
            side="left", padx=8
        )
        body_label(
            tab,
            "Write the codes down before you clear them. The point of clearing "
            "is to see which ones come back after a test drive - those are the "
            "active faults.",
            wraplength=900,
        ).pack(anchor="w", padx=10)
        self.faults_box = _monospace_box(tab, height=18)
        self.faults_box.pack(fill="both", expand=True, padx=10, pady=10)
        _set_text(self.faults_box, "Press \"Read fault codes\" to start.")

    def _read_faults(self) -> None:
        self.app.send("read_faults")
        self._set_status("Reading fault codes ...")

    def _clear_faults(self) -> None:
        if not _confirm_word(
            self, "Clear fault codes",
            "The fault memory will be erased permanently and cannot be "
            "recovered. Read and write down the codes first.",
            "YES",
        ):
            self._set_status("Clearing cancelled - nothing changed.")
            return
        self.app.send("clear_faults")

    def _show_faults(self, codes: Sequence[FaultCode]) -> None:
        self._set_status(f"{len(codes)} fault code(s) read.")
        if not codes:
            _set_text(self.faults_box, "No fault codes stored.")
            return
        lines = [f"{len(codes)} fault code(s):", ""]
        for code in codes:
            marker = "INTERMITTENT" if code.intermittent else "STATIC"
            lines.append(f"{code.number}   {code.text}")
            lines.append(f"          {marker} - {code.status_text}")
            lines.append("")
        _set_text(self.faults_box, "\n".join(lines))

    # -- actuator test -----------------------------------------------------

    def _build_actuator_tab(self) -> None:
        tab = self._new_tab("Actuator test")
        WarningPanel(tab, "The engine must be OFF, ignition ON", [
            "The module switches on one actuator at a time. Valves, relays and "
            "lamps will click and move.",
            "Keep hands, tools and clothing clear of the radiator fan, the belts "
            "and anything else that can move.",
            "The sequence only runs forwards and cannot be stepped back.",
        ]).pack(fill="x", padx=10, pady=10)

        bar = tk.Frame(tab, bg=BACKGROUND)
        bar.pack(fill="x", padx=10)
        self.actuator_button = button(
            bar, "Start the test", self._actuator_step, primary=True, width=18
        )
        self.actuator_button.pack(side="left")

        self.actuator_label = tk.Label(
            tab, text="", bg=BACKGROUND, fg=ACTUAL_COLOUR,
            font=("Segoe UI", 16, "bold"), anchor="w",
        )
        self.actuator_label.pack(fill="x", padx=10, pady=(18, 2))
        self.actuator_detail = body_label(tab, "")
        self.actuator_detail.pack(anchor="w", padx=10)

        self.actuator_box = _monospace_box(tab, height=10)
        self.actuator_box.pack(fill="both", expand=True, padx=10, pady=12)
        _set_text(self.actuator_box, "")

    def _actuator_step(self) -> None:
        if not self.actuator_running:
            if not _confirm_word(
                self, "Actuator test",
                "Make sure the engine is switched off and nobody has their hands "
                "in the engine bay.",
                "YES",
            ):
                return
            self.actuator_running = True
            _set_text(self.actuator_box, "")
            self.actuator_button.configure(text="Next actuator")
        self.app.send("actuator_next")
        self._set_status("Activating the next actuator ...")

    def _show_actuator(self, actuator: Actuator | None) -> None:
        if actuator is None:
            self.actuator_running = False
            self.actuator_button.configure(text="Start the test")
            self.actuator_label.configure(text="Sequence finished", fg=OK_COLOUR)
            self.actuator_detail.configure(
                text="Every actuator has been cycled. Switch the ignition off to "
                     "leave test mode."
            )
            self._set_status("Actuator test finished.")
            return
        self.actuator_label.configure(text=actuator.name, fg=ACTUAL_COLOUR)
        self.actuator_detail.configure(
            text=f"Component code 0x{actuator.code:04X}. "
                 "Listen and feel for it, then press \"Next actuator\"."
        )
        self.actuator_box.configure(state="normal")
        self.actuator_box.insert(
            "end", f"0x{actuator.code:04X}  {actuator.name}\n"
        )
        self.actuator_box.see("end")
        self.actuator_box.configure(state="disabled")
        self._set_status("Actuator active.")

    # -- adaptation and login ----------------------------------------------

    def _build_adaptation_tab(self) -> None:
        tab = self._new_tab("Adaptation")
        WarningPanel(tab, "This writes to the module's permanent memory", [
            "Always read the current value and write it down before you change "
            "anything.",
            "Test a value first - it only takes effect until the ignition is "
            "switched off. Saving is permanent.",
        ]).pack(fill="x", padx=10, pady=(10, 6))

        if self.address in SENSITIVE_ADDRESSES:
            WarningPanel(tab, "Extra caution on this module", [
                f"0x{self.address:02X} {module_name(self.address)}: a wrong value "
                "here can trigger the immobilizer so the car will not start, or "
                "corrupt the odometer and service data.",
                "Fixing that may need a workshop with online coding.",
            ], colour=SPEC_COLOUR).pack(fill="x", padx=10, pady=(0, 6))

        row = tk.Frame(tab, bg=BACKGROUND)
        row.pack(fill="x", padx=10, pady=8)
        body_label(row, "Channel:").pack(side="left")
        self.channel_spin = tk.Spinbox(
            row, from_=0, to=255, width=6, bg=PANEL, fg=TEXT,
            insertbackground=TEXT, relief="flat", buttonbackground=RAISED,
        )
        self.channel_spin.pack(side="left", padx=8)
        button(row, "Read channel", self._read_adaptation, primary=True).pack(
            side="left"
        )

        self.adaptation_label = tk.Label(
            tab, text="No channel read yet.", bg=BACKGROUND, fg=TEXT,
            font=("Segoe UI", 12), anchor="w",
        )
        self.adaptation_label.pack(fill="x", padx=10, pady=(8, 4))

        change = tk.Frame(tab, bg=BACKGROUND)
        change.pack(fill="x", padx=10, pady=4)
        body_label(change, "New value (0-65535):").pack(side="left")
        self.value_entry = tk.Entry(
            change, width=10, bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat",
        )
        self.value_entry.pack(side="left", padx=8)
        button(change, "Test it", self._test_adaptation).pack(side="left")
        button(change, "Save permanently", self._save_adaptation,
               danger=True).pack(side="left", padx=8)

        self.adaptation_box = _monospace_box(tab, height=8)
        self.adaptation_box.pack(fill="both", expand=True, padx=10, pady=10)
        _set_text(self.adaptation_box, "")

        login = tk.Frame(tab, bg=BACKGROUND)
        login.pack(fill="x", padx=10, pady=(0, 12))
        body_label(
            login, "Some channels need a login code first:"
        ).pack(side="left")
        self.login_entry = tk.Entry(
            login, width=10, bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat",
        )
        self.login_entry.pack(side="left", padx=8)
        button(login, "Log in", self._login).pack(side="left")

    def _channel(self) -> int | None:
        try:
            channel = int(self.channel_spin.get())
        except ValueError:
            self._set_status("The channel must be a number between 0 and 255.", True)
            return None
        if not 0 <= channel <= 255:
            self._set_status("The channel must be between 0 and 255.", True)
            return None
        return channel

    def _entered_value(self) -> int | None:
        text = self.value_entry.get().strip()
        if not text:
            self._set_status("Type the new value first.", True)
            return None
        try:
            value = int(text)
        except ValueError:
            self._set_status("The value must be a whole number.", True)
            return None
        if not 0 <= value <= 65535:
            self._set_status("The value must be between 0 and 65535.", True)
            return None
        return value

    def _read_adaptation(self) -> None:
        channel = self._channel()
        if channel is None:
            return
        self.adaptation_tested = None
        self.app.send("read_adaptation", channel)
        self._set_status(f"Reading adaptation channel {channel} ...")

    def _test_adaptation(self) -> None:
        channel, value = self._channel(), self._entered_value()
        if channel is None or value is None:
            return
        if self.adaptation_old is None or self.adaptation_channel != channel:
            self._set_status("Read the channel first so you know the old value.",
                             True)
            return
        self.app.send("test_adaptation", (channel, value))
        self._set_status(f"Testing {value} on channel {channel} ...")

    def _save_adaptation(self) -> None:
        channel, value = self._channel(), self._entered_value()
        if channel is None or value is None:
            return
        if self.adaptation_tested != value:
            self._set_status("Test the value before saving it.", True)
            messagebox.showinfo(
                "Test it first",
                "Press \"Test it\" before saving. A tested value disappears when "
                "you switch the ignition off; a saved one does not.",
                parent=self,
            )
            return
        if not _confirm_word(
            self, "Save adaptation",
            f"Channel {channel} will change from {self.adaptation_old} to "
            f"{value} permanently.",
            "SAVE",
        ):
            self._set_status("Nothing was saved.")
            return
        self.app.send("save_adaptation", (channel, value))

    def _show_adaptation(self, adaptation: Adaptation, kind: str) -> None:
        if kind == "adaptation":
            self.adaptation_channel = adaptation.channel
            self.adaptation_old = adaptation.value
            self.adaptation_label.configure(
                text=f"Channel {adaptation.channel}: current value "
                     f"{adaptation.value}",
                fg=TEXT,
            )
            self.value_entry.delete(0, "end")
            self.value_entry.insert(0, str(adaptation.value))
            self._set_status("Channel read.")
        elif kind == "adaptation_tested":
            self.adaptation_tested = adaptation.value
            self.adaptation_label.configure(
                text=f"Channel {adaptation.channel}: testing "
                     f"{adaptation.value} (was {self.adaptation_old}, not saved)",
                fg=SPEC_COLOUR,
            )
            self._set_status("Value is being tested but has not been saved.")
        else:
            self.adaptation_old = adaptation.value
            self.adaptation_tested = None
            self.adaptation_label.configure(
                text=f"Channel {adaptation.channel}: saved as {adaptation.value}",
                fg=OK_COLOUR,
            )
            self._set_status("Adaptation saved.")

        if adaptation.readings:
            lines = ["Readings reported with the response:", ""]
            lines += [f"   {i}  {r.text}"
                      for i, r in enumerate(adaptation.readings, start=1)]
            _set_text(self.adaptation_box, "\n".join(lines))

    def _login(self) -> None:
        text = self.login_entry.get().strip()
        try:
            code = int(text)
        except ValueError:
            self._set_status("The login code is a number between 0 and 65535.", True)
            return
        if not 0 <= code <= 65535:
            self._set_status("The login code must be between 0 and 65535.", True)
            return
        self.app.send("login", code)
        self._set_status(f"Logging in with {code:05d} ...")

    # -- basic setting -----------------------------------------------------

    def _build_basic_tab(self) -> None:
        tab = self._new_tab("Basic setting")
        WarningPanel(tab, "A basic setting is not just reading values", [
            "The module may drive actuators, reset adaptations and run the "
            "engine on its own.",
            "Only run groups you understand, and follow the repair manual for "
            "your engine code. The wrong group can leave you with a car that "
            "will not start.",
            "Handbrake on, gearbox in neutral.",
        ]).pack(fill="x", padx=10, pady=10)

        row = tk.Frame(tab, bg=BACKGROUND)
        row.pack(fill="x", padx=10, pady=6)
        body_label(row, "Group:").pack(side="left")
        self.basic_entry = tk.Entry(
            row, width=8, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
        )
        self.basic_entry.insert(0, "3")
        self.basic_entry.pack(side="left", padx=8)
        self.basic_button = button(
            row, "Start basic setting", self._toggle_basic, danger=True
        )
        self.basic_button.pack(side="left")

        self.basic_box = _monospace_box(tab, height=14)
        self.basic_box.pack(fill="both", expand=True, padx=10, pady=10)
        _set_text(self.basic_box, "Not running.")
        self.basic_running = False

    def _toggle_basic(self) -> None:
        if self.basic_running:
            self.app.send("basic_setting", False)
            self.app.send("groups", self._selected_groups())
            self.basic_running = False
            self.basic_button.configure(text="Start basic setting")
            _set_text(self.basic_box, "Stopped.")
            return
        try:
            group = int(self.basic_entry.get().strip())
        except ValueError:
            self._set_status("Enter the group number to run.", True)
            return
        if not 1 <= group <= 255:
            self._set_status("The group must be between 1 and 255.", True)
            return
        if not _confirm_word(
            self, "Basic setting",
            f"Group {group} will be started as a basic setting. The module may "
            "move actuators and change stored values.",
            "YES",
        ):
            return
        self.basic_running = True
        self.basic_button.configure(text="Stop basic setting")
        self.app.send("groups", [group])
        self.app.send("basic_setting", True)

    def _show_basic(self, measurement: Measurement) -> None:
        lines = []
        for group, values in measurement.values.items():
            lines.append(f"Group {group:03d} - basic setting running")
            lines.append("")
            for position, value in enumerate(values, start=1):
                name = label(group, position, self.address)
                lines.append(f"   {name[:28]:<28} {value.text:>18}")
            if not values:
                lines.append("   (this module has no such group)")
        _set_text(self.basic_box, "\n".join(lines))

    # -- info --------------------------------------------------------------

    def _build_info_tab(self) -> None:
        tab = self._new_tab("Module info")
        box = _monospace_box(tab, height=22)
        box.pack(fill="both", expand=True, padx=10, pady=10)
        lines = [
            "CONTROL MODULE",
            "",
            f"   Address       0x{self.address:02X}  {self.ident.name}",
            f"   Part number   {self.ident.part_number or '(unknown)'}",
            f"   Component     {self.ident.component or '(unknown)'}",
        ]
        if self.ident.coding is not None:
            lines.append(f"   Coding        {self.ident.coding}")
        if self.ident.wsc is not None:
            lines.append(f"   Workshop      WSC {self.ident.wsc}")
        lines += [
            f"   Key bytes     0x{self.ident.kb1:02X} 0x{self.ident.kb2:02X}",
            "",
            "",
            "WHAT TO LOOK AT ON A 1.9 TDI",
            "",
            "   Group 003   Air mass target vs actual, and the EGR duty cycle.",
            "               At full throttle actual should sit close to target,",
            "               typically 700-900 mg/stroke. Much lower means the",
            "               engine is starving for air: a sooted intake or EGR,",
            "               a boost leak, a blocked air filter, or a tired MAF.",
            "",
            "   Group 004   Start of injection, target vs actual. They should",
            "               agree within about one degree. Persistently retarded",
            "               injection gives white smoke that smells of diesel.",
            "",
            "   Group 011   Charge pressure, target vs actual, plus the N75 duty",
            "               cycle. Actual overshooting target and then collapsing",
            "               is overboost, the usual cause of limp mode. On a high",
            "               mileage TDI that is normally sticking VNT vanes.",
            "",
            "   Group 013   Smooth running, per cylinder. Read it only with a",
            "               fully warm engine at idle. All four should be within",
            "               about +/-2 mg/stroke of zero. One cylinder that is",
            "               always out points at that injector or its compression.",
            "",
            "",
            "A NOTE ON ACCURACY",
            "",
            "   Values marked with a ? use reconstructed conversion formulas.",
            "   The numbers are in the right ballpark but have not been verified",
            "   against ground truth, so check them against VCDS before you spend",
            "   money on parts. The group labels are approximate too - they vary",
            "   between engine codes.",
        ]
        _set_text(box, "\n".join(lines))

    # -- messages ----------------------------------------------------------

    def handle(self, message: object) -> None:
        """Handle one message from the worker thread."""
        if isinstance(message, Measurement):
            if message.basic_setting:
                self._show_basic(message)
            else:
                self._show_measurement(message)
        elif isinstance(message, StatusMessage):
            self._set_status(message.text, message.error)
        elif isinstance(message, Result):
            self._handle_result(message)

    def _handle_result(self, result: Result) -> None:
        if result.kind == "faults":
            self._show_faults(result.payload)  # type: ignore[arg-type]
        elif result.kind == "actuator":
            self._show_actuator(result.payload)  # type: ignore[arg-type]
        elif result.kind in ("adaptation", "adaptation_tested", "adaptation_saved"):
            self._show_adaptation(result.payload, result.kind)  # type: ignore[arg-type]
        elif result.kind == "login":
            if result.payload:
                self._set_status("Login accepted.")
                messagebox.showinfo("Login", "The module accepted the code.",
                                    parent=self)
            else:
                self._set_status("Login was rejected.", True)
                messagebox.showwarning(
                    "Login", "The module rejected that code.", parent=self
                )


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


class App(tk.Tk):
    """The main window. Owns the transport, the workers and the screens."""

    def __init__(
        self,
        settings: Settings | None = None,
        preset_port: str | None = None,
        use_simulator: bool = False,
        address: int = 0x01,
        groups: Sequence[int] = (3, 11),
    ) -> None:
        _enable_dpi_awareness()
        super().__init__()
        self.settings = settings or Settings()
        self.preset_port = preset_port
        self.use_simulator = use_simulator
        self.address = address
        self.groups = list(groups)

        # Point sizes follow tk's scaling; pixel sizes (canvas widgets, window
        # geometry) have to be scaled by hand.
        dpi = self.winfo_fpixels("1i")
        self.tk.call("tk", "scaling", dpi / 72.0)
        self.ui_scale = max(1.0, dpi / 96.0)

        self.title("VAGDIAG")
        self.geometry(f"{self.px(1120)}x{self.px(820)}")
        self.minsize(self.px(900), self.px(640))
        self.configure(bg=BACKGROUND)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self._apply_theme()

        self.queue: queue.Queue[object] = queue.Queue()
        self.transport = None
        self.simulator_link = None
        self.worker: DiagnosticWorker | None = None
        self.scanner: ScanWorker | None = None

        self.container = tk.Frame(self, bg=BACKGROUND)
        self.container.pack(fill="both", expand=True)
        self.screen: ConnectScreen | MainScreen | None = None
        self.show_connect()
        self.after(50, self._pump)

    def px(self, pixels: int) -> int:
        """Scale a pixel measurement for the current display."""
        return int(pixels * self.ui_scale)

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - platform dependent
            pass
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=TEXT,
                        padding=(16, 8), font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", RAISED)],
                  foreground=[("selected", TEXT)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, arrowcolor=TEXT, borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", TEXT)])

    # -- screens -----------------------------------------------------------

    def _swap(self, screen: ConnectScreen | MainScreen) -> None:
        if self.screen is not None:
            self.screen.destroy()
        self.screen = screen
        screen.pack(fill="both", expand=True)

    def show_connect(self) -> None:
        """Show the connection screen."""
        self.title("VAGDIAG")
        self._swap(ConnectScreen(self))

    def show_main(self, ident: Identification) -> None:
        """Show the tabbed main screen for a connected module."""
        self.title(f"VAGDIAG - {ident.name} {ident.part_number}".strip())
        self._swap(MainScreen(self, ident))
        # Push the initial group selection to the worker.
        self.send("groups", self.groups)

    # -- transport ---------------------------------------------------------

    def _open_transport(self, simulator: bool, port: str | None) -> bool:
        """Open the transport. Returns False and reports the problem on failure."""
        try:
            if simulator:
                from .simulator import start_simulator

                self.simulator_link = start_simulator()
                self.transport = self.simulator_link.transport
            else:
                assert port is not None
                self.transport = SerialTransport(port, timeout=self.settings.timeout)
            return True
        except VagdiagError as error:
            self.show_error(error)
            self._close_transport()
            return False

    def _close_transport(self) -> None:
        if self.simulator_link is not None:
            self.simulator_link.close()
            self.simulator_link = None
            self.transport = None
            return
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    # -- workers -----------------------------------------------------------

    def connect(self, simulator: bool, port: str | None, address: int) -> None:
        """Open the transport and start a diagnostic session."""
        self.stop_workers()
        if not self._open_transport(simulator, port):
            if isinstance(self.screen, ConnectScreen):
                self.screen.set_busy(False)
            return
        self.address = address
        assert self.transport is not None
        self.worker = DiagnosticWorker(
            self.transport, self.settings, address, self.groups, self.queue
        )
        self.worker.start()

    def start_scan(self, simulator: bool, port: str | None) -> None:
        """Open the transport and auto-scan every module address."""
        self.stop_workers()
        if not self._open_transport(simulator, port):
            if isinstance(self.screen, ConnectScreen):
                self.screen.set_busy(False)
            return
        assert self.transport is not None
        self.scanner = ScanWorker(self.transport, self.settings, self.queue)
        self.scanner.start()

    def send(self, name: str, data: object = None) -> None:
        """Send a command to the diagnostic worker, if one is running."""
        if self.worker is not None and self.worker.is_alive():
            self.worker.command(name, data)

    def stop_workers(self) -> None:
        """Stop any running worker and wait for it."""
        if self.worker is not None:
            self.worker.command("stop")
            self.worker.shutdown()
            self.worker = None
        if self.scanner is not None:
            self.scanner.shutdown()
            self.scanner = None

    def disconnect(self) -> None:
        """End the session and go back to the connection screen."""
        self.stop_workers()
        self._close_transport()
        self.show_connect()

    # -- message pump ------------------------------------------------------

    def _pump(self) -> None:
        """Drain the worker queue. Runs in the GUI thread via after()."""
        try:
            while True:
                self._route(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _route(self, message: object) -> None:
        if isinstance(message, VagdiagError):
            self._handle_error(message)
            return
        if isinstance(message, Result):
            if message.kind == "connected":
                self.show_main(message.payload)  # type: ignore[arg-type]
                return
            if message.kind == "disconnected":
                return
            if message.kind == "scan_done":
                self.scanner = None
                self._close_transport()
        if self.screen is not None:
            self.screen.handle(message)

    def _handle_error(self, error: VagdiagError) -> None:
        """A worker failed: report it and fall back to the connection screen."""
        self.stop_workers()
        self._close_transport()
        self.show_error(error)
        self.show_connect()

    def show_error(self, error: VagdiagError) -> None:
        """Show an error as a dialog, with the hint spelled out."""
        message = error.message
        if error.hint:
            message = f"{message}\n\n{error.hint}"
        messagebox.showerror("VAGDIAG", message, parent=self)

    # -- shutdown ----------------------------------------------------------

    def quit_app(self) -> None:
        """Close everything down cleanly."""
        self.stop_workers()
        self._close_transport()
        self.destroy()


def run_gui(
    settings: Settings | None = None,
    port: str | None = None,
    use_simulator: bool = False,
    address: int = 0x01,
    groups: Sequence[int] = (3, 11),
) -> int:
    """Start the application. Returns an exit code."""
    try:
        app = App(settings, port, use_simulator, address, groups)
    except tk.TclError as error:  # pragma: no cover - needs a display
        print(f"Could not open a window: {error}")
        print("Use the terminal instead: python -m vagdiag <port>")
        return 2
    app.mainloop()
    return 0
