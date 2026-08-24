"""Transport layer - an abstraction over the K-line.

The protocol code in :mod:`vagdiag.kwp1281` only ever talks to
:class:`Transport`. That lets the exact same protocol stack run against

* a real KKL cable (:class:`SerialTransport`), and
* a virtual ECU inside the same process (:class:`MemoryTransport`),

with no com0com, no pseudo terminals and no other OS dependencies. The test
suite uses the latter.

The K-line is a half-duplex single-wire bus: **everything you send is echoed
back on your own receiver**. The memory transport emulates that exactly (each
written byte lands in both the local and the peer receive queue), so the echo
handling in the protocol layer is genuinely exercised rather than special-cased
away.
"""

from __future__ import annotations

import os
import queue
import sys
import time
from abc import ABC, abstractmethod
from typing import Iterator

from .exceptions import KWPTimeout, TransportError

__all__ = [
    "Transport",
    "SerialTransport",
    "MemoryTransport",
    "create_linked_pair",
    "list_ports",
    "pyserial_available",
    "read_ftdi_latency",
    "set_ftdi_latency",
]

#: Standard baud rate for KWP1281 over the K-line.
BAUD = 10400

#: Bit time for the 5-baud init (one fifth of a second per bit).
BIT_TIME = 0.2


class Transport(ABC):
    """Common interface for anything that can carry KWP1281 bytes."""

    #: True when the transport echoes what you write (a real K-line does).
    echoes: bool = True

    #: Descriptive name, used in logs and error messages.
    name: str = "unknown"

    @abstractmethod
    def write_byte(self, b: int) -> None:
        """Write one byte onto the bus."""

    @abstractmethod
    def read_byte(self, timeout: float) -> int:
        """Read one byte. Raises :class:`KWPTimeout` if nothing arrives in time."""

    @abstractmethod
    def flush_input(self) -> None:
        """Discard whatever is sitting in the receive buffer."""

    @abstractmethod
    def send_5baud_address(self, address: int) -> None:
        """Wake the control module with its address sent at 5 baud."""

    @abstractmethod
    def close(self) -> None:
        """Close the transport."""

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Real serial port (FTDI FT232RL / KKL VAG 409.1)
# ---------------------------------------------------------------------------


class SerialTransport(Transport):
    """K-line over a virtual COM port (KKL cable with an FT232RL).

    ``pyserial`` is imported lazily so that the rest of the package - and the
    whole test suite - works without pyserial installed.
    """

    echoes = True

    def __init__(
        self,
        port: str,
        baud: int = BAUD,
        timeout: float = 1.0,
        bit_time: float = BIT_TIME,
    ) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - dependency check
            raise TransportError(
                "pyserial is not installed. Run: pip install pyserial",
                hint="pip install pyserial   (or: pip install -e .)",
            ) from exc

        self.name = port
        self.bit_time = bit_time
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=2.0,
            )
        except Exception as exc:  # pragma: no cover - needs hardware
            raise TransportError(f"Could not open {port}: {exc}") from exc

        self._timeout = timeout
        #: FTDI latency timer in ms (None when unknown). Advisory only.
        self.latency_ms = read_ftdi_latency(port)

    # -- byte stream -------------------------------------------------------

    def write_byte(self, b: int) -> None:
        try:
            self._serial.write(bytes([b & 0xFF]))
            self._serial.flush()
        except Exception as exc:  # pragma: no cover - needs hardware
            raise TransportError(f"Write error on {self.name}: {exc}") from exc

    def read_byte(self, timeout: float) -> int:
        if timeout != self._timeout:
            self._serial.timeout = timeout
            self._timeout = timeout
        try:
            data = self._serial.read(1)
        except Exception as exc:  # pragma: no cover - needs hardware
            raise TransportError(f"Read error on {self.name}: {exc}") from exc
        if not data:
            raise KWPTimeout(
                f"No answer from the control module within {timeout:.2f} s."
            )
        return data[0]

    def flush_input(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception:  # pragma: no cover - needs hardware
            pass

    # -- 5-baud wake-up ----------------------------------------------------

    def send_5baud_address(self, address: int) -> None:
        """Bit-bang the module address using the break condition, 200 ms per bit.

        Order: start bit (break ON), eight data bits LSB first (0 = break ON,
        1 = break OFF), stop bit (break OFF).
        """
        self.flush_input()
        ser = self._serial
        try:
            ser.break_condition = True          # start bit
            time.sleep(self.bit_time)
            for i in range(8):
                bit = (address >> i) & 1
                ser.break_condition = bit == 0
                time.sleep(self.bit_time)
            ser.break_condition = False         # stop bit
            time.sleep(self.bit_time)
        except Exception as exc:  # pragma: no cover - needs hardware
            raise TransportError(
                f"Could not send the 5-baud address on {self.name}: {exc}"
            ) from exc
        finally:
            try:
                ser.break_condition = False
            except Exception:  # pragma: no cover
                pass
        self.flush_input()

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Virtual in-memory bus (for the simulator and the tests)
# ---------------------------------------------------------------------------


class MemoryTransport(Transport):
    """One end of a virtual K-line bus.

    Written bytes land in *both* the local queue (the echo) and the peer's
    queue, exactly as on a real single-wire bus.
    """

    echoes = True

    def __init__(
        self,
        own: queue.Queue[int],
        peer: queue.Queue[int],
        five_baud: queue.Queue[int],
        is_client: bool,
        name: str = "memory",
    ) -> None:
        self._own = own
        self._peer = peer
        self._five_baud = five_baud
        self._is_client = is_client
        self.name = name
        self.closed = False

    def write_byte(self, b: int) -> None:
        if self.closed:
            raise TransportError("The transport is closed.")
        b &= 0xFF
        self._own.put(b)   # echo on our own receiver
        self._peer.put(b)  # the peer hears the same byte

    def read_byte(self, timeout: float) -> int:
        try:
            return self._own.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise KWPTimeout(
                f"Nothing on the virtual bus within {timeout:.2f} s."
            ) from None

    def flush_input(self) -> None:
        _drain(self._own)

    def send_5baud_address(self, address: int) -> None:
        """Logical equivalent of the 5-baud wake-up.

        The bus is silent while the wake-up runs, so both queues are drained.
        """
        if not self._is_client:
            raise TransportError("Only the client side can send a 5-baud address.")
        _drain(self._own)
        _drain(self._peer)
        _drain(self._five_baud)
        self._five_baud.put(address & 0xFF)

    def read_5baud_address(self, timeout: float) -> int:
        """ECU side: wait for a 5-baud wake-up. Raises KWPTimeout."""
        if self._is_client:
            raise TransportError("Only the ECU side can read a 5-baud address.")
        try:
            return self._five_baud.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise KWPTimeout("No 5-baud wake-up received.") from None

    def close(self) -> None:
        self.closed = True


def _drain(q: queue.Queue[int]) -> None:
    """Empty a queue without blocking."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def create_linked_pair() -> tuple[MemoryTransport, MemoryTransport]:
    """Create (client transport, ECU transport) sharing one virtual bus."""
    client_queue: queue.Queue[int] = queue.Queue()
    ecu_queue: queue.Queue[int] = queue.Queue()
    wake_queue: queue.Queue[int] = queue.Queue()
    client = MemoryTransport(client_queue, ecu_queue, wake_queue, True, "memory:client")
    ecu = MemoryTransport(ecu_queue, client_queue, wake_queue, False, "memory:ecu")
    return client, ecu


# ---------------------------------------------------------------------------
# Port and FTDI latency helpers
# ---------------------------------------------------------------------------


def pyserial_available() -> bool:
    """True when pyserial can be imported."""
    try:
        import serial  # noqa: F401
    except ImportError:
        return False
    return True


def list_ports() -> list[tuple[str, str]]:
    """Return [(port, description), ...]. Empty when pyserial is missing."""
    try:
        from serial.tools import list_ports as _list_ports
    except ImportError:
        return []
    return [(p.device, p.description or "") for p in _list_ports.comports()]


def _ftdi_keys() -> Iterator[tuple[str, object]]:
    """Yield (port name, open registry key) for FTDI devices on Windows.

    The caller is responsible for closing each yielded key.
    """
    if os.name != "nt":  # pragma: no cover - Windows only
        return
    import winreg

    base = r"SYSTEM\CurrentControlSet\Enum\FTDIBUS"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return
    with root:
        i = 0
        while True:
            try:
                device = winreg.EnumKey(root, i)
            except OSError:
                return
            i += 1
            path = f"{base}\\{device}\\0000\\Device Parameters"
            key = None
            for access in (winreg.KEY_READ | winreg.KEY_SET_VALUE, winreg.KEY_READ):
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access)
                    break
                except OSError:
                    continue
            if key is None:
                continue
            try:
                port = str(winreg.QueryValueEx(key, "PortName")[0])
            except OSError:
                key.Close()
                continue
            yield port, key


def read_ftdi_latency(port: str) -> int | None:
    """Read the FTDI latency timer (ms) for a COM port. None when unknown."""
    if os.name != "nt":  # pragma: no cover - Windows only
        return None
    import winreg

    for port_name, key in _ftdi_keys():
        try:
            if port_name.upper() != port.upper():
                continue
            try:
                return int(winreg.QueryValueEx(key, "LatencyTimer")[0])
            except OSError:
                return None
        finally:
            key.Close()  # type: ignore[attr-defined]
    return None


def set_ftdi_latency(port: str, ms: int = 1) -> bool:
    """Try to set the FTDI latency timer to ``ms``.

    Requires administrator rights, and the device must be unplugged and
    replugged (or the machine rebooted) before it takes effect. Returns True on
    a successful write. See the README for the manual route via Device Manager.
    """
    if os.name != "nt":  # pragma: no cover - Windows only
        return False
    import winreg

    for port_name, key in _ftdi_keys():
        try:
            if port_name.upper() != port.upper():
                continue
            try:
                winreg.SetValueEx(key, "LatencyTimer", 0, winreg.REG_DWORD, ms)
                return True
            except OSError:
                return False
        finally:
            key.Close()  # type: ignore[attr-defined]
    return False


def is_windows() -> bool:
    """True when running on Windows (used for OS-specific hints)."""
    return sys.platform.startswith("win")
