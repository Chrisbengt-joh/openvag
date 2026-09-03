"""Background workers for the VAGDIAG application.

Nothing in this module touches tkinter. All ECU communication happens here, on
worker threads, and results travel back to the GUI thread through a
:class:`queue.Queue`. The GUI drains that queue from ``after()``.

That split is what keeps the window responsive: a dead K-line blocks a worker
for a second at a time, never the user interface.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .datalog import CsvLogger
from .exceptions import VagdiagError
from .formulas import Reading
from .kwp1281 import KWP1281, Actuator, Adaptation, ScanResult, scan
from .modules import AUTOSCAN_ADDRESSES, module_name
from .transport import Transport

__all__ = [
    "Settings",
    "Measurement",
    "StatusMessage",
    "Result",
    "DiagnosticWorker",
    "ScanWorker",
]


@dataclass
class Settings:
    """Connection and logging settings shared by the workers."""

    timeout: float = 1.0
    init_timeout: float = 2.0
    baud: int | None = None      # None = detect automatically
    interval: float = 0.0
    delimiter: str = ";"
    decimal_comma: bool = True
    trace: Callable[[str], None] | None = None

    def new_client(self, transport: Transport) -> KWP1281:
        """Create a protocol client configured from these settings."""
        return KWP1281(
            transport,
            timeout=self.timeout,
            init_timeout=self.init_timeout,
            trace=self.trace,
        )


# ---------------------------------------------------------------------------
# Messages sent to the GUI thread
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """One polling cycle."""

    time: float
    values: dict[int, list[Reading]] = field(default_factory=dict)
    basic_setting: bool = False


@dataclass
class StatusMessage:
    """A line for the status bar."""

    text: str
    error: bool = False


@dataclass
class Result:
    """A named result from a one-off command.

    ``kind`` is one of ``faults``, ``actuator``, ``adaptation``,
    ``adaptation_tested``, ``adaptation_saved``, ``login``, ``connected``,
    ``disconnected``, ``scan_progress`` or ``scan_done``.
    """

    kind: str
    payload: object = None


# ---------------------------------------------------------------------------
# Diagnostic worker - owns one session
# ---------------------------------------------------------------------------


class DiagnosticWorker(threading.Thread):
    """Connects to one control module and serves commands until told to stop.

    Attribute names deliberately avoid ``_stop``, ``_handle`` and friends,
    which are private attributes of :class:`threading.Thread` on some Python
    versions and would be silently shadowed.
    """

    def __init__(
        self,
        transport: Transport,
        settings: Settings,
        address: int,
        groups: Sequence[int],
        out: queue.Queue[object],
    ) -> None:
        super().__init__(name="vagdiag-diagnostics", daemon=True)
        self.transport = transport
        self.settings = settings
        self.address = address
        self.groups = list(groups)
        self.out = out
        self.inbox: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stop_event = threading.Event()
        self._logger: CsvLogger | None = None
        self._polling = True
        self._basic_setting = False

    # -- API for the GUI thread -------------------------------------------

    def command(self, name: str, data: object = None) -> None:
        """Queue a command for the worker."""
        self.inbox.put((name, data))

    def shutdown(self) -> None:
        """Ask the worker to finish and wait for it."""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5.0)

    @property
    def logging(self) -> bool:
        """True while a CSV log is open."""
        return self._logger is not None

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        client = None
        try:
            self.out.put(StatusMessage(
                f"Waking {module_name(self.address)} "
                f"(0x{self.address:02X}). This takes a few seconds ..."
            ))
            client = self.settings.new_client(self.transport)
            ident = client.connect(self.address)
            client.start_keepalive()
            self.out.put(Result("connected", ident))
            self.out.put(StatusMessage(f"Connected to {ident.summary()}"))

            while not self._stop_event.is_set():
                self._dispatch_commands(client)
                if self._stop_event.is_set():
                    break
                if not self._polling or not self.groups:
                    client.keep_alive()
                    time.sleep(0.15)
                    continue
                self._poll_once(client)
                if self.settings.interval:
                    time.sleep(self.settings.interval)
        except VagdiagError as error:
            self.out.put(error)
        except Exception as error:  # unexpected - still say something useful
            self.out.put(VagdiagError(
                f"Unexpected error in the diagnostics thread: {error!r}"
            ))
        finally:
            self._close_log()
            if client is not None:
                try:
                    client.disconnect()
                except VagdiagError:
                    pass
            self.out.put(Result("disconnected"))

    def _poll_once(self, client: KWP1281) -> None:
        """Read every selected group once and publish the result."""
        measurement = Measurement(
            time=time.monotonic(), basic_setting=self._basic_setting
        )
        for group in self.groups:
            measurement.values[group] = (
                client.basic_setting(group)
                if self._basic_setting
                else client.read_group(group)
            )
        if self._logger is not None:
            self._logger.log(measurement.values)
        self.out.put(measurement)

    def _dispatch_commands(self, client: KWP1281) -> None:
        """Drain the command queue, reporting failures without dying."""
        while True:
            try:
                name, data = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                if not self._run_command(client, name, data):
                    return
            except VagdiagError:
                raise
            except Exception as error:  # a bad command must not kill the session
                self.out.put(StatusMessage(f"Command failed: {error!r}", error=True))

    def _run_command(self, client: KWP1281, name: str, data: object) -> bool:
        """Run one command. Returns False when the worker should stop."""
        if name == "stop":
            self._stop_event.set()
            return False

        if name == "groups":
            self.groups = list(data)  # type: ignore[arg-type]
            self._restart_log_if_open()
        elif name == "polling":
            self._polling = bool(data)
        elif name == "basic_setting":
            self._basic_setting = bool(data)
            self.out.put(StatusMessage(
                "Basic setting mode active - the module may drive actuators."
                if self._basic_setting else "Back to normal measuring blocks."
            ))
        elif name == "read_faults":
            self.out.put(Result("faults", client.read_faults()))
        elif name == "clear_faults":
            client.clear_faults()
            self.out.put(StatusMessage("Fault memory cleared."))
            self.out.put(Result("faults", client.read_faults()))
        elif name == "actuator_next":
            actuator: Actuator | None = client.actuator_test_next()
            self.out.put(Result("actuator", actuator))
        elif name == "read_adaptation":
            result: Adaptation = client.read_adaptation(int(data))  # type: ignore[arg-type]
            self.out.put(Result("adaptation", result))
        elif name == "test_adaptation":
            channel, value = data  # type: ignore[misc]
            self.out.put(Result(
                "adaptation_tested", client.test_adaptation(channel, value)
            ))
        elif name == "save_adaptation":
            channel, value = data  # type: ignore[misc]
            self.out.put(Result(
                "adaptation_saved", client.save_adaptation(channel, value)
            ))
        elif name == "login":
            self.out.put(Result("login", client.login(int(data))))  # type: ignore[arg-type]
        elif name == "log_start":
            self._start_log(str(data))
        elif name == "log_stop":
            self._close_log()
        return True

    # -- logging -----------------------------------------------------------

    def _start_log(self, filename: str) -> None:
        self._close_log()
        self._logger = CsvLogger(
            filename, self.groups, self.address,
            delimiter=self.settings.delimiter,
            decimal_comma=self.settings.decimal_comma,
        )
        self.out.put(StatusMessage(f"Logging to {filename}"))

    def _restart_log_if_open(self) -> None:
        """The columns depend on the groups, so a running log must be closed."""
        if self._logger is None:
            return
        old = self._logger.filename
        self._close_log()
        self.out.put(StatusMessage(
            f"Groups changed, so the log {old.name} was closed. Start a new one."
        ))

    def _close_log(self) -> None:
        if self._logger is not None:
            name, rows = self._logger.filename, self._logger.rows
            self._logger.close()
            self._logger = None
            self.out.put(StatusMessage(f"Log saved: {name} ({rows} rows)"))


# ---------------------------------------------------------------------------
# Scan worker
# ---------------------------------------------------------------------------


class ScanWorker(threading.Thread):
    """Runs an auto-scan, reporting each module as it is probed."""

    def __init__(
        self,
        transport: Transport,
        settings: Settings,
        out: queue.Queue[object],
        addresses: Sequence[int] = AUTOSCAN_ADDRESSES,
    ) -> None:
        super().__init__(name="vagdiag-scan", daemon=True)
        self.transport = transport
        self.settings = settings
        self.out = out
        self.addresses = list(addresses)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Stop after the module currently being probed."""
        self._cancelled.set()

    def shutdown(self) -> None:
        """Cancel and wait for the thread."""
        self.cancel()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=10.0)

    def run(self) -> None:
        results: list[ScanResult] = []

        def report(entry: ScanResult) -> None:
            results.append(entry)
            self.out.put(Result("scan_progress", entry))

        try:
            for address in self.addresses:
                if self._cancelled.is_set():
                    break
                scan(
                    self.transport,
                    addresses=(address,),
                    attempts=2,
                    timeout=0.6,
                    init_timeout=1.2,
                    pause=0.3,
                    callback=report,
                    trace=self.settings.trace,
                )
        except VagdiagError as error:
            self.out.put(error)
        except Exception as error:  # pragma: no cover - defensive
            self.out.put(VagdiagError(f"Unexpected error during the scan: {error!r}"))
        finally:
            self.out.put(Result("scan_done", results))
