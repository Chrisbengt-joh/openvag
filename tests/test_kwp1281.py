"""Protocol stack tests - run entirely against the ECU simulator, no hardware."""

from __future__ import annotations

import threading
import time

import pytest

from vagdiag.exceptions import (
    KWPConnectionError,
    KWPError,
    KWPProtocolError,
    KWPTimeout,
    VagdiagError,
)
from vagdiag.faults import FaultCode
from vagdiag.kwp1281 import KWP1281, BlockTitle, KWPChannel, scan
from vagdiag.simulator import SIM_LOGIN, start_simulator
from vagdiag.transport import create_linked_pair


@pytest.fixture()
def link():
    """A running simulator with a short session timeout (fast teardown)."""
    sim = start_simulator(session_timeout=1.0)
    try:
        yield sim
    finally:
        sim.close()


@pytest.fixture()
def client(link):
    """A client connected to the simulator."""
    c = KWP1281(link.transport, timeout=0.5, init_timeout=2.0, ack_delay=0.0)
    c.connect(0x01)
    try:
        yield c
    finally:
        c.stop_keepalive()


# ---------------------------------------------------------------------------
# The block format itself
# ---------------------------------------------------------------------------


def test_block_round_trip():
    """A block should survive the trip with its title, data and counter intact."""
    a_transport, b_transport = create_linked_pair()
    a = KWPChannel(a_transport, timeout=1.0, ack_delay=0.0)
    b = KWPChannel(b_transport, timeout=1.0, ack_delay=0.0)

    received = []
    thread = threading.Thread(target=lambda: received.append(b.read_block()))
    thread.start()
    a.send_block(BlockTitle.READ_GROUP, bytes([3]))
    thread.join(timeout=3.0)

    assert len(received) == 1
    block = received[0]
    assert block.title == BlockTitle.READ_GROUP
    assert block.data == bytes([3])
    assert block.counter == 1


def test_block_counter_wraps():
    """The block counter must go 0xFF -> 0x00."""
    transport, _ = create_linked_pair()
    channel = KWPChannel(transport)
    channel.counter = 0xFE
    assert channel.next_counter() == 0xFF
    assert channel.next_counter() == 0x00


# ---------------------------------------------------------------------------
# Connecting and identification
# ---------------------------------------------------------------------------


def test_connect_reads_ident(client):
    """Init should yield key bytes, part number, component name and coding."""
    ident = client.ident
    assert client.connected
    assert ident is not None
    assert ident.kb1 == 0x01 and ident.kb2 == 0x8A
    assert ident.part_number == "1Z9906019"
    assert ident.component == "TDI SIMULATOR"
    assert ident.coding == 1
    assert ident.wsc == 12345
    assert "Engine" in ident.summary()


def test_connect_to_silent_address_raises(link):
    """An address with no module should give a readable error, not a hang."""
    c = KWP1281(link.transport, timeout=0.3, init_timeout=0.3)
    with pytest.raises(KWPConnectionError) as info:
        c.connect(0x02, attempts=1)
    assert info.value.hint


def test_disconnect_ends_the_session(client, link):
    """After 0x06 the simulator should be waiting for a new wake-up."""
    client.disconnect()
    assert not client.connected
    time.sleep(0.1)
    client.connect(0x01)
    assert link.ecu.sessions == 2


# ---------------------------------------------------------------------------
# Fault codes
# ---------------------------------------------------------------------------


def test_read_faults(client):
    """Both simulated fault codes should be read and decoded."""
    codes = client.read_faults()
    assert [c.code for c in codes] == [17965, 560]
    assert isinstance(codes[0], FaultCode)
    assert codes[0].intermittent is True
    assert codes[1].intermittent is False
    assert "Charge pressure" in codes[0].text
    assert "intermittent" in codes[0].status_text


def test_clear_faults(client, link):
    """After clearing, the memory should be empty."""
    client.clear_faults()
    assert link.ecu.clears == 1
    assert client.read_faults() == []


# ---------------------------------------------------------------------------
# Measuring values
# ---------------------------------------------------------------------------


def test_read_group_3(client):
    """Group 3 should give engine speed, air mass SPEC/ACTUAL and EGR duty."""
    values = client.read_group(3)
    assert len(values) == 4
    assert values[0].unit == "rpm"
    assert 800 <= values[0].number <= 3500
    assert values[1].unit == "mg/stroke"
    assert values[2].unit == "mg/stroke"
    assert values[3].unit == "%"


def test_read_group_11_charge_pressure(client):
    """Group 11 should give the charge pressure in mbar."""
    values = client.read_group(11)
    assert values[1].unit == "mbar"
    assert 900 <= values[1].number <= 2200


def test_unknown_group_returns_empty_list(client):
    """A group the module lacks should return an empty list, not raise."""
    assert client.read_group(200) == []


def test_basic_setting(client):
    """A basic setting should return readings just like a normal group."""
    assert len(client.basic_setting(3)) == 4


# ---------------------------------------------------------------------------
# Actuators, adaptation, login
# ---------------------------------------------------------------------------


def test_actuator_sequence(client):
    """The actuator test should step through the sequence and then finish."""
    names = []
    for _ in range(10):
        actuator = client.actuator_test_next()
        if actuator is None:
            break
        names.append(actuator.name)
    assert len(names) == 4
    assert "N18" in names[0]
    assert client.actuator_test_next() is None


def test_adaptation_read_test_save(client, link):
    """Reading gives the stored value, testing changes nothing, saving writes."""
    assert client.read_adaptation(2).value == 100
    assert client.test_adaptation(2, 130).value == 130
    assert link.ecu.adaptation[2] == 100  # testing does not store
    assert client.save_adaptation(2, 130).value == 130
    assert link.ecu.adaptation[2] == 130
    assert client.read_adaptation(2).readings  # the response also carries readings


def test_adaptation_value_out_of_range(client):
    """Values above 65535 must be stopped before they reach the bus."""
    with pytest.raises(VagdiagError):
        client.test_adaptation(1, 70000)


def test_login(client):
    """The right code gives True, a wrong one gives False."""
    assert client.login(SIM_LOGIN) is True
    assert client.login(12345) is False


# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------


def test_keepalive_holds_the_session(link):
    """With the keep-alive thread the session should survive a quiet period."""
    link.ecu.session_timeout = 0.4
    c = KWP1281(link.transport, timeout=0.5, ack_delay=0.0)
    c.connect(0x01)
    c.start_keepalive(interval=0.1)
    try:
        time.sleep(1.2)
        assert c.background_error is None
        assert len(c.read_group(3)) == 4
    finally:
        c.stop_keepalive()
        c.disconnect()


def test_without_keepalive_the_session_dies(link):
    """Without keep-alive the module drops the session - with a readable error."""
    link.ecu.session_timeout = 0.3
    c = KWP1281(link.transport, timeout=0.4, ack_delay=0.0)
    c.connect(0x01)
    time.sleep(0.9)
    with pytest.raises(KWPError) as info:
        c.read_group(3)
    assert info.value.hint
    assert not c.connected


# ---------------------------------------------------------------------------
# Fault injection - the client must fail gracefully, never hang
# ---------------------------------------------------------------------------


def test_dropped_ack_gives_timeout(client, link):
    """A missing acknowledgement byte should raise KWPTimeout and kill the session."""
    link.ecu.faults.drop_next_ack = True
    with pytest.raises(KWPTimeout):
        client.read_group(3)
    assert not client.connected


def test_corrupt_ack_gives_protocol_error(client, link):
    """A wrong complement should be caught immediately."""
    link.ecu.faults.corrupt_next_ack = True
    with pytest.raises(KWPProtocolError) as info:
        client.read_group(3)
    assert "acknowledgement" in str(info.value).lower()


def test_missing_response_gives_timeout(client, link):
    """A module that goes silent after a command should time out."""
    link.ecu.faults.silent_on_next_command = True
    with pytest.raises(KWPTimeout):
        client.read_group(3)


def test_corrupt_etx_gives_protocol_error(client, link):
    """A block not terminated by 0x03 must be rejected."""
    link.ecu.faults.corrupt_etx_in_next_response = True
    with pytest.raises(KWPProtocolError) as info:
        client.read_group(3)
    assert "0x03" in str(info.value)


def test_bad_block_counter_is_detected(client, link):
    """A jumping block counter means the session is out of sync."""
    link.ecu.faults.bad_counter_in_next_response = True
    with pytest.raises(KWPProtocolError) as info:
        client.read_group(3)
    assert "counter" in str(info.value).lower()


def test_no_response_to_init(link):
    """A module that never wakes should raise a connection error with a hint."""
    link.ecu.faults.no_init_response = True
    c = KWP1281(link.transport, timeout=0.3, init_timeout=0.3)
    with pytest.raises(KWPConnectionError):
        c.connect(0x01, attempts=1)


def test_bad_sync_byte(link):
    """A wrong sync byte instead of 0x55 should raise a connection error."""
    link.ecu.faults.bad_sync_byte = True
    c = KWP1281(link.transport, timeout=0.3, init_timeout=0.5)
    with pytest.raises(KWPConnectionError) as info:
        c.connect(0x01, attempts=1)
    assert "0x55" in str(info.value)


def test_client_recovers_after_a_fault(client, link):
    """After a protocol error a fresh connection should work."""
    link.ecu.faults.drop_next_ack = True
    with pytest.raises(KWPTimeout):
        client.read_group(3)
    time.sleep(1.1)  # let the simulator give up on its broken session
    client.connect(0x01)
    assert len(client.read_group(3)) == 4


# ---------------------------------------------------------------------------
# Auto-scan
# ---------------------------------------------------------------------------


def test_scan_only_finds_responding_modules(link):
    """Silent addresses should be reported as silent without stopping the scan."""
    results = scan(
        link.transport,
        addresses=(0x01, 0x02, 0x03),
        attempts=1,
        timeout=0.3,
        init_timeout=0.3,
        pause=0.0,
    )
    assert [r.address for r in results] == [0x01, 0x02, 0x03]
    engine = results[0]
    assert engine.responded is True
    assert engine.ident is not None
    assert len(engine.faults) == 2
    assert all(not r.responded for r in results[1:])
    assert all(r.error for r in results[1:])
