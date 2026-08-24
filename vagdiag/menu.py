"""Terminal user interface for VAGDIAG.

All ECU communication goes through :class:`vagdiag.kwp1281.KWP1281`; this layer
only handles output, input and confirmations. Every command catches
:class:`vagdiag.exceptions.VagdiagError` and shows a readable message with a
hint - the user should never see a traceback.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Callable, Iterable, Sequence

from .datalog import CsvLogger
from .exceptions import VagdiagError
from .faults import FaultCode
from .formulas import Reading
from .kwp1281 import KWP1281, Identification, ScanResult, scan
from .modules import (
    ADDRESSES,
    AUTOSCAN_ADDRESSES,
    SENSITIVE_ADDRESSES,
    group_is_known,
    group_name,
    label,
    module_name,
)
from .transport import Transport

__all__ = ["Menu", "setup_terminal", "enable_ansi", "Keyboard"]

WIDTH = 78

#: True once the terminal is known to handle UTF-8 (set by setup_terminal).
#: The Windows console otherwise runs cp1252, which lacks the box characters,
#: so plain ASCII is used instead.
_UNICODE = False


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def enable_ansi() -> bool:
    """Turn on ANSI escape sequences in the Windows console. True on success."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _enable_utf8() -> bool:
    """Switch the console and the streams to UTF-8. True when fully successful."""
    ok = True
    if os.name == "nt":
        try:
            import ctypes

            if not ctypes.windll.kernel32.SetConsoleOutputCP(65001):  # type: ignore[attr-defined]
                ok = False
        except Exception:
            ok = False
    for stream in (sys.stdout, sys.stderr):
        try:
            # errors="replace" means an odd code page can never crash the
            # program in the middle of a measurement.
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            ok = False
    return ok


def setup_terminal() -> bool:
    """Enable ANSI and UTF-8. Called once at startup."""
    global _UNICODE
    ansi = enable_ansi()
    _UNICODE = _enable_utf8()
    return ansi and _UNICODE


def _box() -> dict[str, str]:
    """Box characters - pretty when the terminal handles UTF-8, else ASCII."""
    if _UNICODE:
        return {"h": "═", "v": "║", "tl": "╔", "tr": "╗", "bl": "╚",
                "br": "╝", "lt": "╠", "rt": "╣", "thin": "─"}
    return {"h": "=", "v": "|", "tl": "+", "tr": "+", "bl": "+",
            "br": "+", "lt": "+", "rt": "+", "thin": "-"}


class Keyboard:
    """Reads single key presses without blocking.

    Used in the live view so that q and l work without pressing Enter. When
    stdin is not a terminal (redirected, for example) None is always returned.
    """

    def __init__(self) -> None:
        self._active = False
        self._saved: object | None = None
        self._fd: int | None = None

    def __enter__(self) -> Keyboard:
        if os.name == "nt":
            self._active = True
            return self
        try:
            import termios
            import tty

            if not sys.stdin.isatty():
                return self
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._active = True
        except Exception:
            self._active = False
        return self

    def __exit__(self, *_exc: object) -> None:
        if os.name != "nt" and self._fd is not None and self._saved is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        self._active = False

    def read(self) -> str | None:
        """Return the pressed key, or None when nothing was pressed."""
        if not self._active:
            return None
        if os.name == "nt":
            try:
                import msvcrt

                if not msvcrt.kbhit():
                    return None
                return msvcrt.getwch()
            except Exception:
                # No real console (redirected stdin) - no keys to read.
                self._active = False
                return None
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        return sys.stdin.read(1)


def clear_screen() -> None:
    """Clear the screen and home the cursor."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def draw(lines: Sequence[str]) -> None:
    """Repaint the screen without flicker.

    The cursor is homed and each line overwrites the existing content
    (``\\x1b[K`` clears the rest of the line). No full clear means no flicker.
    """
    out = ["\x1b[H"]
    for line in lines:
        out.append(line + "\x1b[K\n")
    out.append("\x1b[0J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def heading(text: str) -> str:
    """An underlined heading line."""
    return f"\n{text}\n{_box()['thin'] * min(WIDTH, len(text) + 2)}"


def rule(kind: str = "thin") -> str:
    """A horizontal divider spanning the full width."""
    return _box()[kind] * WIDTH


def warning(lines: Iterable[str]) -> None:
    """Print a prominent warning box."""
    b = _box()
    print()
    print(b["tl"] + b["h"] * (WIDTH - 2) + b["tr"])
    print(b["v"] + " WARNING ".center(WIDTH - 2, " ") + b["v"])
    print(b["lt"] + b["h"] * (WIDTH - 2) + b["rt"])
    for line in lines:
        for chunk in _wrap(line, WIDTH - 4):
            print(b["v"] + " " + chunk.ljust(WIDTH - 4) + " " + b["v"])
    print(b["bl"] + b["h"] * (WIDTH - 2) + b["br"])


def _wrap(text: str, width: int) -> list[str]:
    """Simple word wrapping."""
    if not text:
        return [""]
    lines: list[str] = []
    line = ""
    for word in text.split(" "):
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    return lines


def show_error(error: VagdiagError) -> None:
    """Render an error readably - never as a traceback."""
    print(f"\n  ERROR: {error.message}")
    if error.hint:
        for line in _wrap(f"Hint: {error.hint}", WIDTH - 8):
            print(f"        {line}")
    print()


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def ask(text: str, default: str = "") -> str:
    """Ask for a line of text. An empty line returns the default."""
    prompt = f"{text} [{default}]: " if default else f"{text}: "
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def ask_int(text: str, default: int | None = None,
            low: int = 0, high: int = 65535) -> int | None:
    """Ask for an integer within a range. None when cancelled."""
    while True:
        answer = ask(text, "" if default is None else str(default))
        if not answer:
            return default
        try:
            value = int(answer, 0)
        except ValueError:
            print(f"  '{answer}' is not a number.")
            continue
        if not low <= value <= high:
            print(f"  Enter a number between {low} and {high}.")
            continue
        return value


def ask_exact(text: str, required: str) -> bool:
    """Require the user to type ``required`` exactly (case sensitive)."""
    answer = ask(f"{text} (type {required} to continue)")
    if answer == required:
        return True
    print("  Cancelled - nothing was done.")
    return False


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def format_readings(
    group: int, readings: Sequence[Reading], address: int = 0x01
) -> list[str]:
    """Lines for one measuring block."""
    header = f"Group {group:03d}  {group_name(group, address)}"
    if not group_is_known(group, address):
        header += "  (no labels available)"
    lines = [header]
    if not readings:
        lines.append("    (this module has no such group)")
        return lines
    for i, reading in enumerate(readings, start=1):
        name = label(group, i, address)[:32]
        lines.append(f"    {i}  {name:<32} {reading.text:>22}")
    return lines


def format_faults(codes: Sequence[FaultCode]) -> list[str]:
    """Lines for a list of fault codes."""
    if not codes:
        return ["  No fault codes stored."]
    lines = [f"  {len(codes)} fault code(s) found:", ""]
    for code in codes:
        marker = "INT" if code.intermittent else "   "
        lines.append(f"  {code.number} [{marker}] {code.text}")
        lines.append(f"               {code.status_text}")
        lines.append("")
    return lines


def format_ident(ident: Identification) -> list[str]:
    """Lines for the identification data."""
    lines = [
        f"  Module       0x{ident.address:02X}  {ident.name}",
        f"  Part number  {ident.part_number or '(unknown)'}",
        f"  Component    {ident.component or '(unknown)'}",
    ]
    if ident.coding is not None:
        lines.append(f"  Coding       {ident.coding}")
    if ident.wsc is not None:
        lines.append(f"  Workshop     WSC {ident.wsc}")
    lines.append(f"  Key bytes    0x{ident.kb1:02X} 0x{ident.kb2:02X}")
    if ident.text_lines:
        lines.append("  Raw text     " + " | ".join(ident.text_lines))
    return lines


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


class Menu:
    """Menu-driven terminal interface for a KWP1281 control module."""

    def __init__(
        self,
        transport: Transport,
        trace: Callable[[str], None] | None = None,
        delimiter: str = ";",
        decimal_comma: bool = True,
        interval: float = 0.0,
        timeout: float = 1.0,
        init_timeout: float = 2.0,
    ) -> None:
        self.transport = transport
        self.trace = trace
        self.delimiter = delimiter
        self.decimal_comma = decimal_comma
        self.interval = interval
        self.timeout = timeout
        self.init_timeout = init_timeout
        self.client: KWP1281 | None = None
        setup_terminal()

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        """Run the main loop until the user quits. Returns an exit code."""
        self._welcome()
        try:
            while True:
                try:
                    if not self._main_menu():
                        return 0
                except VagdiagError as error:
                    show_error(error)
                    self.disconnect()
        except KeyboardInterrupt:
            print("\n\nInterrupted.")
            return 130
        finally:
            self.disconnect()

    def _welcome(self) -> None:
        print()
        print(rule("h"))
        print("  VAGDIAG - diagnostics for older VAG cars (KWP1281 over K-line)")
        print(f"  Connection: {self.transport.name}")
        print("  Hobby tool - use at your own risk. VCDS is the reference.")
        print(rule("h"))
        latency = getattr(self.transport, "latency_ms", None)
        if latency is not None and latency > 2:
            warning([
                f"The FTDI latency timer is {latency} ms. KWP1281 is timing "
                "sensitive and communication becomes unreliable above 2 ms.",
                "",
                "Set it to 1 ms: Device Manager -> Ports (COM & LPT) -> your "
                "USB Serial Port -> Properties -> Port Settings -> Advanced -> "
                "Latency Timer (msec) = 1.",
            ])

    def disconnect(self) -> None:
        """Close the session if one is open, ignoring errors."""
        if self.client is not None:
            try:
                self.client.disconnect()
            except VagdiagError:
                pass
            self.client = None

    # -- main menu ---------------------------------------------------------

    def _main_menu(self) -> bool:
        """Show the menu and run the choice. False means quit the program."""
        self._check_background_error()
        print(heading("MAIN MENU"))
        if self.client and self.client.connected and self.client.ident:
            print(f"  Connected: {self.client.ident.summary()}")
            print()
            print("   1  Read fault codes")
            print("   2  Clear fault codes")
            print("   3  Live measuring blocks (with optional CSV log)")
            print("   4  Basic setting")
            print("   5  Actuator test")
            print("   6  Adaptation")
            print("   7  Login")
            print("   8  Show identification")
            print("   9  Disconnect")
            print("   0  Quit")
            choice = ask("\n  Choice")
            return self._run_connected(choice)

        print("  No active session.")
        print()
        print("   1  Auto-scan all control modules")
        print("   2  Connect to a control module")
        print("   3  Show connection information")
        print("   0  Quit")
        choice = ask("\n  Choice")
        if choice == "1":
            self.autoscan()
        elif choice == "2":
            self._connect_prompt()
        elif choice == "3":
            self._show_connection_info()
        elif choice in ("0", "q", "Q"):
            return False
        elif choice:
            print("  Unknown choice.")
        return True

    def _run_connected(self, choice: str) -> bool:
        actions: dict[str, Callable[[], None]] = {
            "1": self._read_faults,
            "2": self._clear_faults,
            "3": self._measuring_blocks,
            "4": self._basic_setting,
            "5": self._actuator_test,
            "6": self._adaptation,
            "7": self._login,
            "8": self._show_ident,
            "9": self.disconnect,
        }
        if choice in ("0", "q", "Q"):
            return False
        action = actions.get(choice)
        if action is None:
            if choice:
                print("  Unknown choice.")
            return True
        action()
        return True

    def _check_background_error(self) -> None:
        """Report an error the keep-alive thread ran into."""
        if self.client is None:
            return
        error = self.client.background_error
        if error is not None and not self.client.connected:
            show_error(error)
            self.client = None

    # -- connecting --------------------------------------------------------

    def new_client(self) -> KWP1281:
        """Create a client configured from the menu's settings."""
        return KWP1281(
            self.transport,
            timeout=self.timeout,
            init_timeout=self.init_timeout,
            trace=self.trace,
        )

    def connect(self, address: int) -> Identification:
        """Connect to a control module and start keep-alive."""
        self.disconnect()
        client = self.new_client()
        print(f"\n  Waking 0x{address:02X} {module_name(address)} "
              f"(the 5-baud init takes about 2 seconds) ...")
        ident = client.connect(address)
        client.start_keepalive()
        self.client = client
        print("  Connected.")
        for line in format_ident(ident):
            print(line)
        return ident

    def _connect_prompt(self) -> None:
        print(heading("CONNECT TO A CONTROL MODULE"))
        for address in AUTOSCAN_ADDRESSES:
            print(f"   {address:02X}  {ADDRESSES.get(address, '')}")
        answer = ask("\n  Module address (hex)", "01")
        try:
            address = int(answer, 16)
        except ValueError:
            print("  Invalid address.")
            return
        if not 0 <= address <= 0xFF:
            print("  The address must be between 00 and FF.")
            return
        self.connect(address)

    def _show_connection_info(self) -> None:
        print(heading("CONNECTION"))
        print(f"  Transport: {self.transport.name}")
        latency = getattr(self.transport, "latency_ms", None)
        if latency is None:
            print("  FTDI latency timer: unknown (check manually, should be 1 ms)")
        else:
            status = "OK" if latency <= 2 else "TOO HIGH - set it to 1 ms"
            print(f"  FTDI latency timer: {latency} ms ({status})")
        print("  Baud rate: 10400, 8N1, half duplex with echo")

    def _show_ident(self) -> None:
        client = self._require_client()
        print(heading("IDENTIFICATION"))
        if client.ident:
            for line in format_ident(client.ident):
                print(line)

    def _require_client(self) -> KWP1281:
        if self.client is None or not self.client.connected:
            raise VagdiagError(
                "No active session.",
                hint="Pick 2 in the main menu and connect to a module first.",
            )
        return self.client

    # -- auto-scan ---------------------------------------------------------

    def autoscan(self) -> list[ScanResult]:
        """Probe every common module address and print a report."""
        print(heading("AUTO-SCAN"))
        print("  Probing every common module address. Most will not answer -")
        print("  that is normal in a car from 1999. Takes a couple of minutes.\n")
        self.disconnect()

        def show(entry: ScanResult) -> None:
            if entry.responded:
                count = len(entry.faults)
                print(f"   0x{entry.address:02X} {entry.name:<28} RESPONDS  "
                      f"{count} fault code(s)")
            else:
                print(f"   0x{entry.address:02X} {entry.name:<28} silent")

        results = scan(
            self.transport,
            addresses=AUTOSCAN_ADDRESSES,
            attempts=2,
            timeout=0.6,
            init_timeout=1.2,
            pause=0.6,
            callback=show,
            trace=self.trace,
        )
        self._scan_report(results)
        return results

    def _scan_report(self, results: Sequence[ScanResult]) -> None:
        """Final report after an auto-scan."""
        responding = [r for r in results if r.responded]
        total = sum(len(r.faults) for r in responding)
        print(heading("SUMMARY"))
        print(f"  Time: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"  {len(responding)} of {len(results)} addresses responded, "
              f"{total} fault code(s) in total.\n")
        for entry in responding:
            assert entry.ident is not None
            print(f"  0x{entry.address:02X} {entry.name}")
            print(f"       {entry.ident.part_number} {entry.ident.component}")
            if entry.error:
                print(f"       ({entry.error})")
            for line in format_faults(entry.faults):
                print("     " + line)
            print()
        if not responding:
            print("  No module answered at all. Check the ignition, the cable,")
            print("  and that the FTDI latency timer is set to 1 ms.")

    # -- fault codes -------------------------------------------------------

    def _read_faults(self) -> None:
        client = self._require_client()
        print(heading("FAULT CODES"))
        for line in format_faults(client.read_faults()):
            print(line)

    def _clear_faults(self) -> None:
        client = self._require_client()
        print(heading("CLEAR FAULT CODES"))
        warning([
            "The fault memory is erased permanently. Read and write down the",
            "codes first - you cannot get them back.",
            "",
            "Never clear a code before you understand what it means. The code",
            "that comes back after a test drive is the interesting one.",
        ])
        if not ask_exact("  Clear the fault memory?", "YES"):
            return
        client.clear_faults()
        print("  Fault memory cleared.")
        remaining = client.read_faults()
        if remaining:
            print("  NOTE: these codes came straight back (active faults):")
            for line in format_faults(remaining):
                print(line)

    # -- measuring blocks --------------------------------------------------

    def _ask_groups(self, default: str = "3 4 11") -> list[int]:
        answer = ask("  Groups (space separated, 1-255)", default)
        groups: list[int] = []
        for chunk in answer.replace(",", " ").split():
            try:
                number = int(chunk, 10)
            except ValueError:
                print(f"  Skipping '{chunk}' - not a number.")
                continue
            if 1 <= number <= 255:
                groups.append(number)
            else:
                print(f"  Skipping {number} - outside 1-255.")
        return groups

    def _measuring_blocks(self) -> None:
        client = self._require_client()
        print(heading("LIVE MEASURING BLOCKS"))
        groups = self._ask_groups()
        if not groups:
            print("  No valid groups given.")
            return
        log_file = ask("  CSV log file (empty = no logging)")
        self.live(client, groups, log_file or None)

    def live(
        self,
        client: KWP1281,
        groups: Sequence[int],
        log_file: str | None = None,
        basic_setting: bool = False,
    ) -> None:
        """Live view of measuring blocks with optional CSV logging."""
        address = client.address or 0x01
        logger: CsvLogger | None = None
        if log_file:
            logger = CsvLogger(
                log_file, groups, address,
                delimiter=self.delimiter, decimal_comma=self.decimal_comma,
            )
        clear_screen()
        start = time.monotonic()
        cycles = 0
        try:
            with Keyboard() as keyboard:
                while True:
                    readings: dict[int, list[Reading]] = {}
                    for group in groups:
                        readings[group] = (
                            client.basic_setting(group)
                            if basic_setting
                            else client.read_group(group)
                        )
                    cycles += 1
                    if logger is not None:
                        logger.log(readings)
                    draw(self._live_lines(
                        address, groups, readings, logger, start, cycles,
                        basic_setting,
                    ))
                    key = keyboard.read()
                    if key in ("q", "Q", "\x03", "\x1b"):
                        break
                    if key in ("l", "L"):
                        logger = self._toggle_log(logger, groups, address)
                    if self.interval:
                        time.sleep(self.interval)
        finally:
            if logger is not None:
                logger.close()
                print(f"\n  Log saved: {logger.filename} ({logger.rows} rows)")
            print()

    def _toggle_log(
        self, logger: CsvLogger | None, groups: Sequence[int], address: int
    ) -> CsvLogger | None:
        """Start or stop logging from the l key."""
        if logger is not None:
            logger.close()
            return None
        name = f"vagdiag_{datetime.now():%Y%m%d_%H%M%S}.csv"
        return CsvLogger(
            name, groups, address,
            delimiter=self.delimiter, decimal_comma=self.decimal_comma,
        )

    def _live_lines(
        self,
        address: int,
        groups: Sequence[int],
        readings: dict[int, list[Reading]],
        logger: CsvLogger | None,
        start: float,
        cycles: int,
        basic_setting: bool,
    ) -> list[str]:
        """Build the live-view screen."""
        elapsed = time.monotonic() - start
        mode = "BASIC SETTING" if basic_setting else "MEASURING BLOCKS"
        log_text = (
            f"log: {logger.filename.name} ({logger.rows} rows)"
            if logger is not None else "log: off"
        )
        lines = [
            f" VAGDIAG {mode}  0x{address:02X} {module_name(address)}",
            f" {datetime.now():%H:%M:%S}   {elapsed:6.1f} s   cycle {cycles:<6d} "
            f"{cycles / elapsed if elapsed else 0:4.1f}/s   {log_text}",
            rule(),
        ]
        for group in groups:
            lines.extend(format_readings(group, readings.get(group, []), address))
            lines.append("")
        lines.append(rule())
        lines.append(" [q] back to the menu    [l] start/stop logging")
        lines.append(" Values marked ? come from reconstructed formulas -"
                     " verify against VCDS.")
        return lines

    # -- basic setting -----------------------------------------------------

    def _basic_setting(self) -> None:
        client = self._require_client()
        print(heading("BASIC SETTING"))
        warning([
            "A basic setting is not the same as reading measuring values.",
            "The module may drive actuators, reset adaptations and run the",
            "engine on its own.",
            "",
            "Only run groups you understand. Always follow the repair manual",
            "for your engine code - the wrong group can leave you with a car",
            "that will not start. Handbrake on, gearbox in neutral.",
        ])
        if not ask_exact("  Continue to the basic setting?", "YES"):
            return
        groups = self._ask_groups("3")
        if not groups:
            return
        log_file = ask("  CSV log file (empty = no logging)")
        self.live(client, groups, log_file or None, basic_setting=True)

    # -- actuator test -----------------------------------------------------

    def _actuator_test(self) -> None:
        client = self._require_client()
        print(heading("ACTUATOR TEST"))
        warning([
            "THE ENGINE MUST BE OFF, ignition ON.",
            "",
            "The module activates one actuator at a time: valves, relays and",
            "lamps will click and move. Keep hands, tools and clothing away",
            "from the radiator fan, the belts and anything else that moves.",
            "",
            "The sequence only runs forwards and cannot be stepped back.",
            "Abort with q.",
        ])
        if not ask_exact("  Start the actuator test?", "YES"):
            return
        number = 0
        while True:
            actuator = client.actuator_test_next()
            if actuator is None:
                print("\n  Sequence finished - every actuator has been cycled.")
                return
            number += 1
            print(f"\n  {number:2d}. {actuator.name}")
            print(f"      component code 0x{actuator.code:04X}")
            answer = ask("      [Enter] next, [q] abort")
            if answer.lower().startswith("q"):
                print("\n  Aborted. Switch the ignition off to leave test mode.")
                return

    # -- adaptation --------------------------------------------------------

    def _adaptation(self) -> None:
        client = self._require_client()
        address = client.address or 0
        print(heading("ADAPTATION"))
        warning([
            "Adaptation changes values in the module's permanent memory.",
            "Always read and write down the old value before you save.",
            "Always test a value (0x22) before you store it (0x2A).",
        ])
        if address in SENSITIVE_ADDRESSES:
            warning([
                f"EXTRA WARNING: 0x{address:02X} {module_name(address)}.",
                "",
                "A wrong adaptation here can trigger the immobilizer so the car",
                "will not start, or corrupt the odometer and service data.",
                "Fixing that may require a workshop with online coding.",
                "",
                "Only continue if you know exactly what the channel does.",
            ])
            if not ask_exact("  Continue anyway?", "I UNDERSTAND"):
                return

        channel = ask_int("  Channel (0-255)", 1, 0, 255)
        if channel is None:
            return
        current = client.read_adaptation(channel)
        print(f"\n  Channel {channel}: current value = {current.value}")
        for i, reading in enumerate(current.readings, start=1):
            print(f"      reading {i}: {reading.text}")

        new_value = ask_int("\n  New value to TEST (empty = cancel)", None)
        if new_value is None:
            print("  Cancelled - nothing changed.")
            return
        tested = client.test_adaptation(channel, new_value)
        print(f"\n  Testing {tested.value} (not stored).")
        for i, reading in enumerate(tested.readings, start=1):
            print(f"      reading {i}: {reading.text}")
        print(f"\n  Old value: {current.value}   New value: {tested.value}")

        if not ask_exact("  Store permanently?", "SAVE"):
            print(f"  Nothing stored. The old value {current.value} applies "
                  "once the ignition is switched off.")
            return
        saved = client.save_adaptation(channel, new_value)
        print(f"  Stored. Channel {channel} = {saved.value} "
              f"(was {current.value}).")

    # -- login -------------------------------------------------------------

    def _login(self) -> None:
        client = self._require_client()
        print(heading("LOGIN"))
        print("  A five-digit login code is required for some adaptations and tests.")
        code = ask_int("  Login code (0-65535, empty = cancel)", None)
        if code is None:
            return
        if client.login(code):
            print(f"  Login {code:05d} accepted.")
        else:
            print(f"  Login {code:05d} was rejected by the module.")
