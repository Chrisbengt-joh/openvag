"""Virtual KWP1281 ECU for development and testing without a car.

The simulator is wired in through :func:`vagdiag.transport.create_linked_pair`,
i.e. directly in memory - no com0com, no pseudo terminals, no real serial port.
It runs exactly the same block and acknowledgement logic as the client
(:class:`vagdiag.kwp1281.KWPChannel`), so the whole protocol stack is genuinely
exercised.

The simulator pretends to be the engine module of a 1.9 TDI with a VP37 pump
and serves

* identification blocks ``1Z9906019 TDI SIMULATOR``,
* measuring blocks 1-13 with values that vary realistically over time,
* fault codes 17965 (intermittent) and 00560,
* an actuator sequence and a handful of adaptation channels.

Faults can also be injected (dropped acknowledgement, silence, corrupt block)
to verify that the client handles broken sessions gracefully.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import formulas
from .exceptions import KWPTimeout, VagdiagError
from .kwp1281 import ETX, Block, BlockTitle, KWPChannel
from .transport import MemoryTransport, create_linked_pair

__all__ = [
    "FaultInjection",
    "SimulatedECU",
    "start_simulator",
    "SimulatorLink",
    "SIM_FAULTS",
    "SIM_ACTUATORS",
    "SIM_LOGIN",
]

#: Key bytes the simulator sends after the 5-baud wake-up.
KEY_BYTE_1 = 0x01
KEY_BYTE_2 = 0x8A

#: Fault codes in the simulated memory: (code, status).
SIM_FAULTS: list[tuple[int, int]] = [
    (17965, 0xAA),  # charge pressure positive deviation, intermittent
    (560, 0x2A),    # EGR control limit, static
]

#: Actuator sequence the simulator steps through.
SIM_ACTUATORS: list[int] = [0x0102, 0x0103, 0x0101, 0x010C]

#: Initial values for the adaptation channels.
SIM_ADAPTATION: dict[int, int] = {0: 0, 1: 32768, 2: 100, 3: 15000, 4: 1}

#: Login code the simulator accepts.
SIM_LOGIN = 11463


@dataclass
class FaultInjection:
    """One-shot flags used to provoke failures in the client.

    Every ``*_next_*`` flag clears itself once it has been used.
    """

    no_init_response: bool = False
    bad_sync_byte: bool = False
    drop_next_ack: bool = False
    corrupt_next_ack: bool = False
    silent_on_next_command: bool = False
    corrupt_etx_in_next_response: bool = False
    bad_counter_in_next_response: bool = False


class _ECUChannel(KWPChannel):
    """Block channel that can inject protocol faults."""

    def __init__(
        self, transport: MemoryTransport, faults: FaultInjection, **kw: object
    ) -> None:
        super().__init__(transport, **kw)  # type: ignore[arg-type]
        self.faults = faults

    def _acknowledge(self, b: int) -> None:
        if self.faults.drop_next_ack:
            self.faults.drop_next_ack = False
            return  # silence - the client should time out
        if self.faults.corrupt_next_ack:
            self.faults.corrupt_next_ack = False
            if self.ack_delay:
                time.sleep(self.ack_delay)
            self.write_raw(b)  # wrong complement
            return
        super()._acknowledge(b)

    def send_block(self, title: int, data: bytes | list[int] = b"") -> Block:
        if self.faults.corrupt_etx_in_next_response:
            self.faults.corrupt_etx_in_next_response = False
            return self._send_block_raw(title, bytes(data), etx=0x00)
        if self.faults.bad_counter_in_next_response:
            self.faults.bad_counter_in_next_response = False
            return self._send_block_raw(title, bytes(data), counter_offset=7)
        return super().send_block(title, data)

    def _send_block_raw(
        self, title: int, data: bytes, etx: int = ETX, counter_offset: int = 0
    ) -> Block:
        """Send a block with a deliberate fault in the counter or the ETX byte."""
        length = 3 + len(data)
        counter = (self.next_counter() + counter_offset) & 0xFF
        frame = bytes([length, counter, title & 0xFF]) + data
        for b in frame:
            self.write_raw(b)
            self.read_raw()  # the client's acknowledgement, deliberately ignored
        self.write_raw(etx)
        return Block(title & 0xFF, data, counter)


class SimulatedECU(threading.Thread):
    """A virtual ECU answering KWP1281 over a :class:`MemoryTransport`."""

    def __init__(
        self,
        transport: MemoryTransport,
        address: int = 0x01,
        faults: FaultInjection | None = None,
        part_number: str = "1Z9906019 ",
        component: str = "TDI SIMULATOR   ",
        coding: int = 1,
        wsc: int = 12345,
        session_timeout: float = 5.0,
        byte_timeout: float = 1.0,
        ack_delay: float = 0.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(name="simulated-ecu", daemon=True)
        self.transport = transport
        self.address = address
        self.faults = faults or FaultInjection()
        self.part_number = part_number
        self.component = component
        self.coding = coding
        self.wsc = wsc
        self.session_timeout = session_timeout
        self.byte_timeout = byte_timeout
        self.ack_delay = ack_delay
        self.clock = clock or time.monotonic
        self._t0 = self.clock()

        self._stop_event = threading.Event()
        self.fault_memory: list[tuple[int, int]] = list(SIM_FAULTS)
        self.adaptation: dict[int, int] = dict(SIM_ADAPTATION)
        self.logged_in = False
        self.sessions = 0
        self.clears = 0
        self._actuator_index = 0
        self.last_error: str = ""

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Ask the simulator thread to finish and join it."""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3.0)

    def run(self) -> None:  # pragma: no cover - thread start covered indirectly
        while not self._stop_event.is_set():
            try:
                address = self.transport.read_5baud_address(0.1)
            except KWPTimeout:
                continue
            except VagdiagError:
                return
            if address != self.address:
                continue  # no module at that address
            try:
                self._session()
            except VagdiagError as exc:
                self.last_error = str(exc)
            except Exception as exc:  # protect the thread from surprises
                self.last_error = f"internal simulator fault: {exc!r}"

    # -- session -----------------------------------------------------------

    def _session(self) -> None:
        """Run a complete diagnostic session from wake-up to shutdown."""
        if self.faults.no_init_response:
            self.faults.no_init_response = False
            return

        channel = _ECUChannel(
            self.transport,
            self.faults,
            timeout=self.byte_timeout,
            ack_delay=self.ack_delay,
        )
        self.sessions += 1
        self._actuator_index = 0
        self.logged_in = False

        time.sleep(0.01)
        channel.write_raw(0x00 if self.faults.bad_sync_byte else 0x55)
        if self.faults.bad_sync_byte:
            self.faults.bad_sync_byte = False
            return
        channel.write_raw(KEY_BYTE_1)
        channel.write_raw(KEY_BYTE_2)

        complement = channel.read_raw(2.0)
        if complement != (~KEY_BYTE_2) & 0xFF:
            return  # wrong answer - abort silently, just like a real module

        for data in self._ident_blocks():
            channel.send_block(BlockTitle.IDENT, data)
            channel.read_block()  # the client's ACK
        channel.send_block(BlockTitle.ACK)

        # Wait for commands in short slices so the thread can shut down quickly
        # and still drop the session when the bus has been quiet for too long -
        # exactly what a real module does without keep-alive.
        last = time.monotonic()
        while not self._stop_event.is_set():
            try:
                block = channel.read_block(0.05)
            except KWPTimeout:
                if time.monotonic() - last > self.session_timeout:
                    return
                continue
            last = time.monotonic()
            if not self._dispatch(channel, block):
                return

    def _ident_blocks(self) -> list[bytes]:
        """Payloads of the identification blocks."""
        coding_block = bytes(
            [
                (self.coding >> 14) & 0x7F,
                (self.coding >> 7) & 0x7F,
                self.coding & 0x7F,
                (self.wsc >> 7) & 0x7F,
                self.wsc & 0x7F,
            ]
        )
        return [
            self.part_number.encode("latin-1"),
            self.component.encode("latin-1"),
            coding_block,
        ]

    # -- command handling --------------------------------------------------

    def _dispatch(self, channel: _ECUChannel, block: Block) -> bool:
        """Answer one command block. Returns False when the session should end."""
        if self.faults.silent_on_next_command:
            self.faults.silent_on_next_command = False
            return True  # no answer - the client gets a timeout

        title = block.title
        data = block.data

        if title == BlockTitle.END_SESSION:
            return False

        if title == BlockTitle.ACK:
            channel.send_block(BlockTitle.ACK)
            return True

        if title == BlockTitle.READ_FAULTS:
            self._send_faults(channel)
            return True

        if title == BlockTitle.CLEAR_FAULTS:
            self.fault_memory.clear()
            self.clears += 1
            channel.send_block(BlockTitle.ACK)
            return True

        if title in (BlockTitle.READ_GROUP, BlockTitle.BASIC_SETTING):
            group = data[0] if data else 1
            values = self.group_data(group)
            if not values:
                channel.send_block(BlockTitle.ACK)
            else:
                channel.send_block(BlockTitle.GROUP_READING, values)
            return True

        if title == BlockTitle.ACTUATOR_TEST:
            if self._actuator_index < len(SIM_ACTUATORS):
                code = SIM_ACTUATORS[self._actuator_index]
                self._actuator_index += 1
                channel.send_block(
                    BlockTitle.ACTUATOR_RESPONSE,
                    bytes([(code >> 8) & 0xFF, code & 0xFF]),
                )
            else:
                channel.send_block(BlockTitle.ACK)
            return True

        if title == BlockTitle.READ_ADAPTATION:
            number = data[0] if data else 0
            self._send_adaptation(channel, number, self.adaptation.get(number, 0))
            return True

        if title == BlockTitle.TEST_ADAPTATION:
            number = data[0] if data else 0
            value = (data[1] << 8) | data[2] if len(data) >= 3 else 0
            self._send_adaptation(channel, number, value)
            return True

        if title == BlockTitle.SAVE_ADAPTATION:
            number = data[0] if data else 0
            value = (data[1] << 8) | data[2] if len(data) >= 3 else 0
            self.adaptation[number] = value
            self._send_adaptation(channel, number, value)
            return True

        if title == BlockTitle.LOGIN:
            code = (data[0] << 8) | data[1] if len(data) >= 2 else 0
            self.logged_in = code == SIM_LOGIN
            channel.send_block(
                BlockTitle.ACK if self.logged_in else BlockTitle.NOT_AVAILABLE
            )
            return True

        # Unknown command - the module answers "not available".
        channel.send_block(BlockTitle.NOT_AVAILABLE)
        return True

    def _send_faults(self, channel: _ECUChannel) -> None:
        """Send the fault memory, at most four codes per block."""
        if not self.fault_memory:
            channel.send_block(BlockTitle.FAULT_CODES, bytes([0xFF, 0xFF, 0x88]))
            channel.read_block()  # the client's ACK
            channel.send_block(BlockTitle.ACK)
            return
        entries = list(self.fault_memory)
        for start in range(0, len(entries), 4):
            chunk = entries[start:start + 4]
            data = bytearray()
            for code, status in chunk:
                data += bytes([(code >> 8) & 0xFF, code & 0xFF, status & 0xFF])
            channel.send_block(BlockTitle.FAULT_CODES, bytes(data))
            channel.read_block()  # the client's ACK
        channel.send_block(BlockTitle.ACK)

    def _send_adaptation(
        self, channel: _ECUChannel, number: int, value: int
    ) -> None:
        """Send a 0xE6 response carrying the channel, the value and three readings."""
        data = bytearray([number & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
        for triplet in (
            formulas.encode(1, self._engine_speed(), a=100),
            formulas.encode(5, 85.0, a=10),
            formulas.encode(6, 13.8, a=100),
        ):
            data += bytes(triplet)
        channel.send_block(BlockTitle.ADAPTATION_RESPONSE, bytes(data))

    # -- simulated measuring values ----------------------------------------

    def _elapsed(self) -> float:
        """Seconds since the simulator started."""
        return self.clock() - self._t0

    def _engine_speed(self) -> float:
        """Engine speed swinging between idle and roughly 3350 rpm."""
        phase = math.sin(self._elapsed() / 7.0)
        return 850.0 + 1250.0 * (phase + 1.0)

    def _throttle(self) -> float:
        """Normalised throttle position 0-1 derived from the speed profile."""
        return max(0.0, min(1.0, (self._engine_speed() - 850.0) / 2500.0))

    def group_data(self, group: int) -> bytes:
        """Build the payload of a measuring block (four values of three bytes)."""
        rpm = self._engine_speed()
        throttle = self._throttle()
        t = self._elapsed()

        if group == 1:
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(51, 8.0 + 42.0 * throttle, a=40),
                formulas.encode(5, 84.0 + 2.0 * math.sin(t / 11.0), a=10),
                formulas.encode(2, 100.0 * throttle, a=200),
            ]
        elif group == 2:
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(51, 8.0 + 42.0 * throttle, a=40),
                formulas.encode(2, 100.0 * throttle, a=200),
                (16, 0xFF, 0b00000011 if throttle < 0.05 else 0b00000001),
            ]
        elif group == 3:
            # Air mass SPEC/ACTUAL in mg/stroke plus the EGR duty cycle. The
            # simulated car reads a little low on ACTUAL at full throttle, just
            # like a car with a sooted-up EGR system.
            spec = 320.0 + 560.0 * throttle
            actual = spec * (0.97 - 0.16 * throttle)
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(51, spec, a=40),
                formulas.encode(51, actual, a=40),
                formulas.encode(2, max(0.0, 62.0 - 60.0 * throttle), a=200),
            ]
        elif group == 4:
            spec = 2.0 + 9.5 * throttle
            actual = spec - 0.4 * throttle
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(4, spec, a=20),
                formulas.encode(4, actual, a=20),
                formulas.encode(2, 30.0 + 40.0 * throttle, a=200),
            ]
        elif group == 11:
            spec = 1000.0 + 1050.0 * throttle
            actual = spec - 90.0 * throttle * throttle
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(18, spec, a=200),
                formulas.encode(18, actual, a=200),
                formulas.encode(2, 20.0 + 65.0 * throttle, a=200),
            ]
        elif group == 13:
            deviations = [
                1.4 * math.sin(t / 3.0),
                -0.6 + 0.3 * math.sin(t / 5.0),
                0.2 * math.sin(t / 4.0),
                -0.9 + 0.4 * math.sin(t / 6.0),
            ]
            # a=2 gives 0.2 mg/stroke per step - fine enough to spot a cylinder
            # deviating by more than +/-2 mg/stroke.
            values = [formulas.encode(39, v, a=2) for v in deviations]
        elif group in (5, 6, 7, 8, 9, 10, 12):
            values = [
                formulas.encode(1, rpm, a=100),
                formulas.encode(51, 8.0 + 42.0 * throttle, a=40),
                formulas.encode(5, 84.0, a=10),
                formulas.encode(6, 13.9 - 0.4 * throttle, a=100),
            ]
        else:
            return b""  # this module has no such group

        data = bytearray()
        for triplet in values:
            data += bytes(triplet)
        return bytes(data)


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------


@dataclass
class SimulatorLink:
    """A running simulator plus the client transport that talks to it."""

    transport: MemoryTransport
    ecu: SimulatedECU

    def close(self) -> None:
        """Stop the simulator and close the transport."""
        self.ecu.stop()
        self.transport.close()

    def __enter__(self) -> SimulatorLink:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def start_simulator(address: int = 0x01, **kw: object) -> SimulatorLink:
    """Start a virtual ECU and return the link to it."""
    client, ecu_transport = create_linked_pair()
    ecu = SimulatedECU(ecu_transport, address=address, **kw)  # type: ignore[arg-type]
    ecu.start()
    return SimulatorLink(transport=client, ecu=ecu)
