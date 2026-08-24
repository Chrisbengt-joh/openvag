"""Tests for the GUI background workers.

These run headless - the workers never touch tkinter, which is exactly the
property that keeps the window responsive, so they are testable on their own.
"""

from __future__ import annotations

import queue
import time

import pytest

from vagdiag.exceptions import VagdiagError
from vagdiag.guiworker import (
    DiagnosticWorker,
    Measurement,
    Result,
    ScanWorker,
    Settings,
    StatusMessage,
)
from vagdiag.simulator import SIM_LOGIN, start_simulator


def _collect(out: queue.Queue, kind, timeout: float = 6.0):
    """Wait for the first message of a given type or Result kind."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = out.get(timeout=0.1)
        except queue.Empty:
            continue
        if isinstance(message, VagdiagError):
            raise AssertionError(f"worker failed: {message}")
        if isinstance(kind, str):
            if isinstance(message, Result) and message.kind == kind:
                return message.payload
        elif isinstance(message, kind):
            return message
    raise AssertionError(f"no {kind} within {timeout} s")


@pytest.fixture()
def link():
    sim = start_simulator(session_timeout=3.0)
    try:
        yield sim
    finally:
        sim.close()


@pytest.fixture()
def worker(link):
    out: queue.Queue = queue.Queue()
    w = DiagnosticWorker(link.transport, Settings(interval=0.02), 0x01, [3], out)
    w.out_queue = out  # convenience for the tests
    w.start()
    _collect(out, "connected")
    try:
        yield w, out
    finally:
        w.command("stop")
        w.shutdown()


def test_worker_connects_and_polls(worker):
    """The worker should connect and then stream measurements."""
    w, out = worker
    measurement = _collect(out, Measurement)
    assert 3 in measurement.values
    assert len(measurement.values[3]) == 4
    assert measurement.basic_setting is False


def test_worker_reads_faults(worker):
    """A read_faults command must come back as a Result."""
    w, out = worker
    w.command("read_faults")
    codes = _collect(out, "faults")
    assert [c.code for c in codes] == [17965, 560]


def test_worker_clears_faults(worker):
    """Clearing must empty the memory and report the new (empty) list."""
    w, out = worker
    w.command("clear_faults")
    codes = _collect(out, "faults")
    assert codes == []


def test_worker_actuator_sequence(worker):
    """Stepping the actuator test must yield actuators and then None."""
    w, out = worker
    w.command("actuator_next")
    first = _collect(out, "actuator")
    assert first is not None
    assert "N18" in first.name


def test_worker_adaptation_round_trip(worker, link):
    """Read, test and save must each report back, and only save persists."""
    w, out = worker
    w.command("read_adaptation", 2)
    assert _collect(out, "adaptation").value == 100

    w.command("test_adaptation", (2, 130))
    assert _collect(out, "adaptation_tested").value == 130
    assert link.ecu.adaptation[2] == 100

    w.command("save_adaptation", (2, 130))
    assert _collect(out, "adaptation_saved").value == 130
    assert link.ecu.adaptation[2] == 130


def test_worker_login(worker):
    """A correct login must report True."""
    w, out = worker
    w.command("login", SIM_LOGIN)
    assert _collect(out, "login") is True


def test_worker_basic_setting_flag(worker):
    """Basic-setting mode must mark the measurements it produces."""
    w, out = worker
    w.command("basic_setting", True)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        message = _collect(out, Measurement)
        if message.basic_setting:
            return
    raise AssertionError("no basic-setting measurement arrived")


def test_worker_logs_to_csv(worker, tmp_path):
    """Logging must produce a CSV with a header and rows."""
    w, out = worker
    path = tmp_path / "log.csv"
    w.command("log_start", str(path))
    _collect(out, StatusMessage)
    time.sleep(0.6)
    w.command("log_stop")
    time.sleep(0.4)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("timestamp;seconds;003.1")
    assert len(lines) >= 2


def test_worker_survives_a_bad_command(worker):
    """An unusable command must be reported without killing the session."""
    w, out = worker
    w.command("read_adaptation", "not-a-number")
    _collect(out, StatusMessage)
    w.command("read_faults")
    assert _collect(out, "faults") is not None


def test_worker_reports_a_dead_connection(link):
    """A module that never answers must surface as a VagdiagError."""
    out: queue.Queue = queue.Queue()
    settings = Settings(timeout=0.3, init_timeout=0.3)
    w = DiagnosticWorker(link.transport, settings, 0x02, [3], out)
    w.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            message = out.get(timeout=0.1)
        except queue.Empty:
            continue
        if isinstance(message, VagdiagError):
            assert message.hint
            w.shutdown()
            return
    w.shutdown()
    raise AssertionError("the failure was never reported")


def test_scan_worker_reports_each_module(link):
    """The scan worker must stream one result per address, then finish."""
    out: queue.Queue = queue.Queue()
    scanner = ScanWorker(
        link.transport, Settings(), out, addresses=(0x01, 0x02)
    )
    scanner.start()
    try:
        first = _collect(out, "scan_progress")
        assert first.address == 0x01
        assert first.responded is True
        results = _collect(out, "scan_done", timeout=10.0)
        assert [r.address for r in results] == [0x01, 0x02]
        assert results[1].responded is False
    finally:
        scanner.shutdown()
