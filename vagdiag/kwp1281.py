"""KWP1281 over the K-line - a clean protocol implementation, no user interface.

The protocol in brief
---------------------

1. **5-baud init**: the module address is sent as break pulses, 200 ms per bit.
   The module answers ``0x55``, key byte 1, key byte 2. After roughly 30 ms we
   reply with the complement of key byte 2.
2. **Block format**: ``[length][block counter][title][data...][0x03]`` where
   ``length = 3 + number of data bytes``.
3. **Byte acknowledgement**: every byte except the trailing ``0x03`` is
   acknowledged by the receiver with its complement. We insert a small delay
   before our acknowledgement so that slow modules can keep up.
4. **The block counter** is shared by both parties and increments by one per
   block (0xFF wraps to 0x00).
5. **Keep-alive**: if more than about a second passes without traffic the module
   drops the session, so an ACK block (0x09) has to be sent regularly.

Choice of keep-alive strategy
-----------------------------
Keep-alive runs in a **background thread** (:meth:`KWP1281.start_keepalive`).
The reason is that both the terminal and the GUI have states where the main
thread blocks on keyboard input (menu choices, confirmation words) - a pump in
the main loop would have dropped the session there. All block traffic is guarded
by an ``RLock`` so the thread can never land in the middle of another command.
If you need a strictly single-threaded mode, call :meth:`KWP1281.keep_alive`
yourself.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterable, Sequence

from .exceptions import (
    KWPConnectionError,
    KWPError,
    KWPProtocolError,
    KWPRejected,
    KWPTimeout,
    VagdiagError,
)
from .faults import FaultCode, actuator_name, decode_block
from .formulas import Reading, compute
from .modules import AUTOSCAN_ADDRESSES, module_name
from .transport import AUTO_BAUDS
from .transport import Transport

__all__ = [
    "BlockTitle",
    "Block",
    "KWPChannel",
    "KWP1281",
    "Identification",
    "Adaptation",
    "Actuator",
    "ScanResult",
    "scan",
    "ETX",
]

#: Terminating byte of every block.
ETX = 0x03


def _hexlist(values: list[int]) -> str:
    return " ".join(f"0x{v:02X}" for v in values)


#: Sync byte the module sends after a successful 5-baud wake-up.
SYNC = 0x55


class BlockTitle(IntEnum):
    """Block titles (commands and responses) in KWP1281."""

    # Commands we send
    ACTUATOR_TEST = 0x04
    CLEAR_FAULTS = 0x05
    END_SESSION = 0x06
    READ_FAULTS = 0x07
    ACK = 0x09
    RECODE = 0x10             # deliberately NOT implemented in v1 - too risky
    READ_ADAPTATION = 0x21
    TEST_ADAPTATION = 0x22
    BASIC_SETTING = 0x28
    READ_GROUP = 0x29
    SAVE_ADAPTATION = 0x2A
    LOGIN = 0x2B

    # Responses from the module
    ADAPTATION_RESPONSE = 0xE6
    GROUP_READING = 0xE7
    NOT_AVAILABLE = 0xF4
    ACTUATOR_RESPONSE = 0xF5
    IDENT = 0xF6
    CODING = 0xF7
    FAULT_CODES = 0xFC


def _title_name(title: int) -> str:
    """Readable name of a block title (for errors and debug output)."""
    try:
        return BlockTitle(title).name
    except ValueError:
        return f"0x{title:02X}"


@dataclass(frozen=True)
class Block:
    """One received or transmitted KWP1281 block."""

    title: int
    data: bytes = b""
    counter: int = 0

    @property
    def title_name(self) -> str:
        """The block title in plain text."""
        return _title_name(self.title)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        hexdata = " ".join(f"{b:02X}" for b in self.data)
        return f"<Block {self.title_name} ctr={self.counter} data=[{hexdata}]>"


# ---------------------------------------------------------------------------
# Shared byte and block layer
# ---------------------------------------------------------------------------


class KWPChannel:
    """Byte and block layer for KWP1281.

    Used by both the client (:class:`KWP1281`) and the ECU simulator, so the
    tests exercise exactly the same acknowledgement and echo logic that real
    hardware meets.
    """

    def __init__(
        self,
        transport: Transport,
        timeout: float = 1.0,
        ack_delay: float = 0.0015,
        check_echo: bool = True,
        check_counter: bool = True,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.ack_delay = ack_delay
        self.check_echo = check_echo
        self.check_counter = check_counter
        self.counter = 0
        self.last_traffic = time.monotonic()
        self._counter_synced = False
        self._trace = trace

    # -- byte level --------------------------------------------------------

    def _write_byte(self, b: int) -> None:
        """Write one byte and consume the K-line echo."""
        b &= 0xFF
        self.transport.write_byte(b)
        self.last_traffic = time.monotonic()
        if not self.transport.echoes:
            return
        echo = self.transport.read_byte(self.timeout)
        if self.check_echo and echo != b:
            raise KWPProtocolError(
                f"Echo mismatch: sent 0x{b:02X} but got 0x{echo:02X} back. "
                "The K-line cable or the latency setting is the likely cause."
            )

    #: Echo/garbage bytes tolerated while waiting for the sync byte.
    MAX_INIT_JUNK = 8

    def _read_byte(self, timeout: float | None = None) -> int:
        """Read one byte from the bus."""
        b = self.transport.read_byte(self.timeout if timeout is None else timeout)
        self.last_traffic = time.monotonic()
        return b

    def _acknowledge(self, b: int) -> None:
        """Answer with the complement of the received byte."""
        if self.ack_delay:
            time.sleep(self.ack_delay)
        self._write_byte((~b) & 0xFF)

    def write_raw(self, b: int) -> None:
        """Write a byte outside the block structure (used during init)."""
        self._write_byte(b)

    def read_raw(self, timeout: float | None = None) -> int:
        """Read a byte outside the block structure (used during init)."""
        return self._read_byte(timeout)

    # -- block level -------------------------------------------------------

    def next_counter(self) -> int:
        """Advance the block counter (0xFF wraps to 0x00) and return it."""
        self.counter = (self.counter + 1) & 0xFF
        return self.counter

    def send_block(self, title: int, data: bytes | Sequence[int] = b"") -> Block:
        """Send a block, waiting for the receiver's acknowledgement of each byte."""
        payload = bytes(data)
        length = 3 + len(payload)
        if length > 0xFF:
            raise KWPProtocolError("Block too long for KWP1281 (max 252 data bytes).")
        counter = self.next_counter()
        frame = bytes([length, counter, title & 0xFF]) + payload
        for b in frame:
            self._write_byte(b)
            ack = self._read_byte()
            if ack != (~b) & 0xFF:
                raise KWPProtocolError(
                    f"Bad acknowledgement for byte 0x{b:02X}: expected "
                    f"0x{(~b) & 0xFF:02X}, got 0x{ack:02X}."
                )
        self._write_byte(ETX)
        block = Block(title & 0xFF, payload, counter)
        if self._trace:
            self._trace(f"-> {block}")
        return block

    def read_block(self, timeout: float | None = None) -> Block:
        """Read a block, acknowledging every byte except the trailing 0x03."""
        length = self._read_byte(timeout)
        if length < 3:
            raise KWPProtocolError(
                f"Invalid block length {length} - expected at least 3. "
                "The session is out of sync."
            )
        self._acknowledge(length)

        counter = self._read_byte()
        self._acknowledge(counter)
        if self.check_counter and self._counter_synced:
            expected = (self.counter + 1) & 0xFF
            if counter != expected:
                raise KWPProtocolError(
                    f"Block counter jumped: expected {expected}, got {counter}. "
                    "The session is out of sync."
                )

        title = self._read_byte()
        self._acknowledge(title)

        data = bytearray()
        for _ in range(length - 3):
            b = self._read_byte()
            self._acknowledge(b)
            data.append(b)

        etx = self._read_byte()
        if etx != ETX:
            raise KWPProtocolError(
                f"Block terminated with 0x{etx:02X} instead of 0x03."
            )

        self.counter = counter
        self._counter_synced = True
        block = Block(title, bytes(data), counter)
        if self._trace:
            self._trace(f"<- {block}")
        return block


# ---------------------------------------------------------------------------
# Client data types
# ---------------------------------------------------------------------------


@dataclass
class Identification:
    """Identification data the module sends right after init."""

    address: int
    kb1: int = 0
    kb2: int = 0
    part_number: str = ""
    component: str = ""
    coding: int | None = None
    wsc: int | None = None
    text_lines: list[str] = field(default_factory=list)
    raw_blocks: list[bytes] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Name of the control module."""
        return module_name(self.address)

    def summary(self) -> str:
        """A compact one-line description of the module."""
        parts = [f"0x{self.address:02X} {self.name}"]
        if self.part_number:
            parts.append(self.part_number)
        if self.component:
            parts.append(self.component)
        if self.coding is not None:
            parts.append(f"coding {self.coding}")
        if self.wsc is not None:
            parts.append(f"WSC {self.wsc}")
        return " | ".join(parts)


@dataclass(frozen=True)
class Adaptation:
    """Response to an adaptation command (0x21/0x22/0x2A)."""

    channel: int
    value: int
    readings: list[Reading] = field(default_factory=list)


@dataclass(frozen=True)
class Actuator:
    """One actuator in the actuator test sequence."""

    code: int
    raw: bytes

    @property
    def name(self) -> str:
        """The actuator's name from the database."""
        return actuator_name(self.code)


@dataclass
class ScanResult:
    """Result of probing one control module during an auto-scan."""

    address: int
    responded: bool = False
    ident: Identification | None = None
    faults: list[FaultCode] = field(default_factory=list)
    error: str = ""

    @property
    def name(self) -> str:
        """Name of the control module."""
        return module_name(self.address)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class KWP1281(KWPChannel):
    """Diagnostic client for one KWP1281 control module."""

    #: Maximum number of identification blocks accepted before giving up.
    MAX_IDENT_BLOCKS = 40

    def __init__(
        self,
        transport: Transport,
        timeout: float = 1.0,
        init_timeout: float = 2.0,
        ack_delay: float = 0.0015,
        keepalive_interval: float = 0.8,
        trace: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(transport, timeout=timeout, ack_delay=ack_delay, trace=trace)
        self.init_timeout = init_timeout
        self.keepalive_interval = keepalive_interval
        self.connected = False
        self.address: int | None = None
        self.ident: Identification | None = None
        self._lock = threading.RLock()
        self._ka_thread: threading.Thread | None = None
        self._ka_stop = threading.Event()
        self._ka_error: KWPError | None = None

    # -- connection --------------------------------------------------------

    def connect(self, address: int, attempts: int = 2) -> Identification:
        """Wake the module at ``address`` and read its identification blocks.

        When the transport allows it (``auto_baud``), a module that answers
        at the wrong baud rate makes us switch to the next rate in
        :data:`AUTO_BAUDS` and try again. The transport keeps the rate that
        worked, so later connects start there.
        """
        last: KWPError | None = None
        bauds_tried: set[int] = set()
        number = 0
        while number < max(1, attempts):
            number += 1
            try:
                return self._connect_once(address)
            except KWPConnectionError as exc:
                last = exc
                self.connected = False
                if exc.wrong_baud and self._switch_baud(bauds_tried):
                    number -= 1                 # does not use up an attempt
                    continue
            except KWPError as exc:
                last = exc
                self.connected = False
            if number < attempts:
                time.sleep(0.5)
        assert last is not None
        raise last

    def _switch_baud(self, tried: set[int]) -> bool:
        """Move the transport to the next untried rate. False when done."""
        transport = self.transport
        if not transport.auto_baud or transport.baud is None:
            return False
        tried.add(transport.baud)
        for baud in AUTO_BAUDS:
            if baud not in tried:
                break
        else:
            return False
        # Let the module finish its retries and drop back to idle before we
        # wake it again, otherwise the second init lands in the middle of it.
        self._drain(1.5)
        if not transport.set_baud(baud):
            return False
        if self._trace:
            self._trace(f"switching to {baud} baud")
        return True

    def _drain(self, quiet: float) -> None:
        """Read and discard until the line has been silent for ``quiet`` s."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                self.transport.read_byte(quiet)
            except KWPError:
                return

    def _connect_once(self, address: int) -> Identification:
        with self._lock:
            self.stop_keepalive()
            self.connected = False
            self.counter = 0
            self._counter_synced = False
            self.transport.flush_input()
            self.transport.send_5baud_address(address)

            # Wait for the sync byte. On a real K-line our own break shows
            # up as echo garbage (0x00 and the like) before the module's 0x55,
            # so skip a handful of junk bytes rather than flushing blindly.
            junk: list[int] = []
            deadline = time.monotonic() + self.init_timeout
            while True:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise KWPTimeout("init timeout")
                    sync = self._read_byte(remaining)
                except KWPTimeout as exc:
                    if junk:
                        raise KWPConnectionError(
                            f"Module 0x{address:02X} ({module_name(address)}) "
                            f"sent {_hexlist(junk)} but never the sync byte 0x55. "
                            "It may talk at 9600 baud instead of 10400 "
                            "(--baud 9600), or the K-line is noisy.",
                            wrong_baud=True,
                        ) from exc
                    raise KWPConnectionError(
                        f"No answer from module 0x{address:02X} "
                        f"({module_name(address)}) after the 5-baud wake-up."
                    ) from exc
                if sync == SYNC:
                    break
                junk.append(sync)
                if len(junk) > self.MAX_INIT_JUNK:
                    raise KWPConnectionError(
                        f"Expected sync byte 0x55 from 0x{address:02X}, "
                        f"got {_hexlist(junk)}. The module may talk at 9600 "
                        "baud instead of 10400 (--baud 9600).",
                        wrong_baud=True,
                    )

            kb1 = self._read_byte(self.init_timeout)
            kb2 = self._read_byte(self.init_timeout)

            time.sleep(0.03)
            self._write_byte((~kb2) & 0xFF)

            ident = Identification(address=address, kb1=kb1, kb2=kb2)
            self._read_ident_blocks(ident)

            self.address = address
            self.ident = ident
            self.connected = True
            self._ka_error = None
            return ident

    def _read_ident_blocks(self, ident: Identification) -> None:
        """Read identification blocks (0xF6) until the module sends an ACK."""
        for _ in range(self.MAX_IDENT_BLOCKS):
            block = self.read_block(self.init_timeout)
            if block.title == BlockTitle.ACK:
                return
            ident.raw_blocks.append(block.data)
            if block.title in (BlockTitle.IDENT, BlockTitle.CODING):
                self._parse_ident_block(ident, block.data)
            self.send_block(BlockTitle.ACK)
        raise KWPProtocolError(
            "The control module never stopped sending identification blocks."
        )

    @staticmethod
    def _parse_ident_block(ident: Identification, data: bytes) -> None:
        """Interpret an identification block as text or as a coding block."""
        if not data:
            return
        printable = all(32 <= b < 127 for b in data)
        if printable:
            text = data.decode("latin-1").strip()
            if not text:
                return
            ident.text_lines.append(text)
            if not ident.part_number and _looks_like_part_number(text):
                ident.part_number = text
            elif not ident.component:
                ident.component = text
            return
        # Coding block: 7-bit packing of the coding plus the workshop code.
        if len(data) >= 5:
            ident.coding = ((data[0] & 0x7F) << 14) | ((data[1] & 0x7F) << 7) | (
                data[2] & 0x7F
            )
            ident.wsc = ((data[3] & 0x7F) << 7) | (data[4] & 0x7F)

    def disconnect(self) -> None:
        """End the session cleanly (block 0x06). Errors are ignored."""
        self.stop_keepalive()
        with self._lock:
            if self.connected:
                try:
                    self.send_block(BlockTitle.END_SESSION)
                except VagdiagError:
                    pass
            self.connected = False
            self.address = None

    def __enter__(self) -> KWP1281:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()

    # -- keep-alive --------------------------------------------------------

    def keep_alive(self) -> None:
        """Send one ACK block to keep the session alive."""
        self._command(BlockTitle.ACK, expected=(BlockTitle.ACK,))

    def start_keepalive(self, interval: float | None = None) -> None:
        """Start the background thread that keeps the session alive."""
        if interval is not None:
            self.keepalive_interval = interval
        if self._ka_thread and self._ka_thread.is_alive():
            return
        self._ka_stop.clear()
        self._ka_thread = threading.Thread(
            target=self._keepalive_loop, name="kwp1281-keepalive", daemon=True
        )
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        """Stop the keep-alive thread and join it."""
        thread = self._ka_thread
        self._ka_stop.set()
        self._ka_thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _keepalive_loop(self) -> None:
        """Send ACK blocks whenever the bus has been quiet for too long."""
        while not self._ka_stop.wait(0.05):
            if not self.connected:
                continue
            if time.monotonic() - self.last_traffic < self.keepalive_interval:
                continue
            try:
                with self._lock:
                    if not self.connected or self._ka_stop.is_set():
                        continue
                    if time.monotonic() - self.last_traffic < self.keepalive_interval:
                        continue
                    self.send_block(BlockTitle.ACK)
                    self.read_block()
            except VagdiagError as exc:
                self.connected = False
                self._ka_error = exc if isinstance(exc, KWPError) else KWPError(str(exc))
                return

    @property
    def background_error(self) -> KWPError | None:
        """Error the keep-alive thread ran into, if any."""
        return self._ka_error

    # -- commands ----------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.connected:
            if self._ka_error is not None:
                raise self._ka_error
            raise KWPError(
                "No active session - connect to the control module first.",
                hint="Switch the ignition off for five seconds, back on, and "
                "reconnect.",
            )

    def _command(
        self,
        title: int,
        data: bytes | Sequence[int] = b"",
        expected: Iterable[int] | None = None,
        timeout: float | None = None,
    ) -> Block:
        """Send a command block and read the response."""
        with self._lock:
            self._require_connected()
            try:
                self.send_block(title, data)
                response = self.read_block(timeout)
            except KWPError:
                self.connected = False
                raise
            if expected is not None and response.title not in tuple(expected):
                wanted = ", ".join(_title_name(t) for t in expected)
                raise KWPRejected(
                    f"The module answered {response.title_name} to "
                    f"{_title_name(title)} (expected {wanted})."
                )
            return response

    # -- fault codes -------------------------------------------------------

    def read_faults(self) -> list[FaultCode]:
        """Read all fault codes. Multiple 0xFC blocks are handled and acked."""
        with self._lock:
            self._require_connected()
            codes: list[FaultCode] = []
            try:
                self.send_block(BlockTitle.READ_FAULTS)
                for _ in range(64):
                    block = self.read_block()
                    if block.title == BlockTitle.ACK:
                        break
                    if block.title != BlockTitle.FAULT_CODES:
                        raise KWPRejected(
                            f"Unexpected response {block.title_name} while "
                            "reading fault codes."
                        )
                    codes.extend(decode_block(block.data))
                    self.send_block(BlockTitle.ACK)
                else:
                    raise KWPProtocolError(
                        "The control module never stopped sending fault codes."
                    )
            except KWPError:
                self.connected = False
                raise
            return codes

    def clear_faults(self) -> None:
        """Clear the fault memory (block 0x05)."""
        self._command(BlockTitle.CLEAR_FAULTS, expected=(BlockTitle.ACK,))

    # -- measuring values --------------------------------------------------

    @staticmethod
    def _parse_readings(data: bytes) -> list[Reading]:
        """Split a payload into three-byte groups and convert them."""
        readings: list[Reading] = []
        for i in range(0, len(data) - 2, 3):
            readings.append(compute(data[i], data[i + 1], data[i + 2]))
        return readings

    def read_group(self, group: int) -> list[Reading]:
        """Read a measuring block (0x29). Empty list when the group is absent."""
        response = self._command(
            BlockTitle.READ_GROUP,
            bytes([group & 0xFF]),
            expected=(BlockTitle.GROUP_READING, BlockTitle.ACK,
                      BlockTitle.NOT_AVAILABLE),
        )
        if response.title != BlockTitle.GROUP_READING:
            return []
        return self._parse_readings(response.data)

    def basic_setting(self, group: int) -> list[Reading]:
        """Start/read a basic setting for a group (block 0x28).

        WARNING: a basic setting can drive actuators and reset adaptations.
        """
        response = self._command(
            BlockTitle.BASIC_SETTING,
            bytes([group & 0xFF]),
            expected=(
                BlockTitle.GROUP_READING,
                BlockTitle.ADAPTATION_RESPONSE,
                BlockTitle.ACK,
                BlockTitle.NOT_AVAILABLE,
            ),
        )
        if response.title == BlockTitle.GROUP_READING:
            return self._parse_readings(response.data)
        if response.title == BlockTitle.ADAPTATION_RESPONSE and len(response.data) > 3:
            return self._parse_readings(response.data[3:])
        return []

    # -- actuator test -----------------------------------------------------

    def actuator_test_next(self) -> Actuator | None:
        """Step to the next actuator (block 0x04). None when the sequence ends."""
        response = self._command(
            BlockTitle.ACTUATOR_TEST,
            expected=(BlockTitle.ACTUATOR_RESPONSE, BlockTitle.ACK,
                      BlockTitle.NOT_AVAILABLE),
            timeout=max(self.timeout, 2.0),
        )
        if response.title != BlockTitle.ACTUATOR_RESPONSE or len(response.data) < 2:
            return None
        code = (response.data[0] << 8) | response.data[1]
        if code == 0:
            return None
        return Actuator(code=code, raw=response.data)

    # -- adaptation --------------------------------------------------------

    @staticmethod
    def _parse_adaptation(response: Block, channel: int) -> Adaptation:
        """Interpret a 0xE6 response as an :class:`Adaptation`."""
        if len(response.data) < 3:
            raise KWPProtocolError(
                f"Adaptation response too short ({len(response.data)} bytes)."
            )
        return Adaptation(
            channel=response.data[0] if response.data[0] else channel,
            value=(response.data[1] << 8) | response.data[2],
            readings=KWP1281._parse_readings(response.data[3:]),
        )

    def read_adaptation(self, channel: int) -> Adaptation:
        """Read an adaptation channel (block 0x21)."""
        response = self._command(
            BlockTitle.READ_ADAPTATION,
            bytes([channel & 0xFF]),
            expected=(BlockTitle.ADAPTATION_RESPONSE,),
        )
        return self._parse_adaptation(response, channel)

    def test_adaptation(self, channel: int, value: int) -> Adaptation:
        """Test an adaptation value without storing it (block 0x22)."""
        _require_16bit(value)
        response = self._command(
            BlockTitle.TEST_ADAPTATION,
            bytes([channel & 0xFF, (value >> 8) & 0xFF, value & 0xFF]),
            expected=(BlockTitle.ADAPTATION_RESPONSE,),
        )
        return self._parse_adaptation(response, channel)

    def save_adaptation(self, channel: int, value: int) -> Adaptation:
        """Store an adaptation value permanently (block 0x2A).

        WARNING: this writes to the module's EEPROM. On the instrument cluster
        (0x17) and the immobilizer (0x25) a wrong value can immobilise the car.
        """
        _require_16bit(value)
        response = self._command(
            BlockTitle.SAVE_ADAPTATION,
            bytes([channel & 0xFF, (value >> 8) & 0xFF, value & 0xFF]),
            expected=(BlockTitle.ADAPTATION_RESPONSE, BlockTitle.ACK),
        )
        if response.title == BlockTitle.ACK:
            return Adaptation(channel=channel, value=value)
        return self._parse_adaptation(response, channel)

    # -- login -------------------------------------------------------------

    def login(self, code: int) -> bool:
        """Log in with a five-digit code (block 0x2B). True on ACK."""
        _require_16bit(code)
        response = self._command(
            BlockTitle.LOGIN,
            bytes([(code >> 8) & 0xFF, code & 0xFF, 0x00]),
            expected=(BlockTitle.ACK, BlockTitle.NOT_AVAILABLE),
        )
        return response.title == BlockTitle.ACK


def _require_16bit(value: int) -> None:
    """Check that a value fits in 0-65535."""
    if not 0 <= value <= 0xFFFF:
        raise VagdiagError(
            f"The value {value} is outside the allowed range 0-65535.",
            hint="Enter a five-digit number between 0 and 65535.",
        )


def _looks_like_part_number(text: str) -> bool:
    """Rough heuristic for VAG part numbers, for example ``028906021AB``."""
    compact = text.replace(" ", "")
    return (
        8 <= len(compact) <= 14
        and compact[0].isdigit()
        and sum(c.isdigit() for c in compact) >= 6
        and compact.isalnum()
    )


# ---------------------------------------------------------------------------
# Auto-scan
# ---------------------------------------------------------------------------


def scan(
    transport: Transport,
    addresses: Iterable[int] = AUTOSCAN_ADDRESSES,
    attempts: int = 2,
    timeout: float = 0.6,
    init_timeout: float = 1.2,
    pause: float = 0.6,
    callback: Callable[[ScanResult], None] | None = None,
    trace: Callable[[str], None] | None = None,
) -> list[ScanResult]:
    """Probe each module address: connect, fetch ident, count faults, disconnect.

    Addresses that do not answer are reported as silent - it is entirely normal
    for most of them to be absent in a car from 1999.
    """
    results: list[ScanResult] = []
    for address in addresses:
        entry = ScanResult(address=address)
        client = KWP1281(
            transport,
            timeout=timeout,
            init_timeout=init_timeout,
            trace=trace,
        )
        try:
            entry.ident = client.connect(address, attempts=attempts)
            entry.responded = True
            try:
                entry.faults = client.read_faults()
            except VagdiagError as exc:
                entry.error = f"Fault codes could not be read: {exc}"
        except VagdiagError as exc:
            entry.error = str(exc)
        finally:
            try:
                client.disconnect()
            except VagdiagError:
                pass
            transport.flush_input()
        results.append(entry)
        if callback:
            callback(entry)
        if pause:
            time.sleep(pause)
    return results
