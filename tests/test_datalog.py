"""Tests for the CSV logging."""

from __future__ import annotations

from vagdiag.datalog import CsvLogger
from vagdiag.formulas import compute


def _measurement():
    """One polling cycle covering groups 3 and 11."""
    return {
        3: [compute(1, 100, 100), compute(51, 40, 20),
            compute(51, 40, 17), compute(2, 200, 50)],
        11: [compute(1, 100, 100), compute(18, 200, 225),
             compute(18, 200, 220), compute(2, 200, 100)],
    }


def test_header_and_row(tmp_path):
    """The header must name the group, position, label and unit."""
    path = tmp_path / "log.csv"
    with CsvLogger(path, [3, 11]) as logger:
        logger.log(_measurement())
        assert logger.rows == 1

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 2
    columns = lines[0].split(";")
    assert columns[0] == "timestamp"
    assert columns[1] == "seconds"
    assert columns[2] == "003.1 Engine speed [rpm]"
    assert columns[3] == "003.2 Air mass SPEC [mg/stroke]"
    assert columns[6] == "011.1 Engine speed [rpm]"
    assert len(columns) == 2 + 8


def test_decimal_comma_is_the_default(tmp_path):
    """European Excel wants semicolons and a decimal comma."""
    path = tmp_path / "log.csv"
    with CsvLogger(path, [3]) as logger:
        logger.log({3: _measurement()[3]})
    row = path.read_text(encoding="utf-8-sig").splitlines()[1]
    assert ";2000,000;" in row


def test_dot_format(tmp_path):
    """--csv-dot gives comma delimiters and a decimal point."""
    path = tmp_path / "log.csv"
    with CsvLogger(path, [3], delimiter=",", decimal_comma=False) as logger:
        logger.log({3: _measurement()[3]})
    row = path.read_text(encoding="utf-8-sig").splitlines()[1]
    assert ",2000.000," in row


def test_missing_values_give_empty_cells(tmp_path):
    """A group the module lacks must give empty cells, not a shorter row."""
    path = tmp_path / "log.csv"
    with CsvLogger(path, [3, 200]) as logger:
        logger.log({3: _measurement()[3], 200: []})
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines[0].split(";")) == len(lines[1].split(";")) == 10
    assert lines[1].endswith(";;;;")


def test_text_values_are_logged_as_text(tmp_path):
    """Bit fields and ASCII values must reach the log as text."""
    path = tmp_path / "log.csv"
    with CsvLogger(path, [2]) as logger:
        logger.log({2: [compute(16, 255, 0b10100101)]})
    assert "10100101" in path.read_text(encoding="utf-8-sig").splitlines()[1]


def test_every_row_is_flushed(tmp_path):
    """The log must be readable while it is still open."""
    path = tmp_path / "log.csv"
    logger = CsvLogger(path, [3])
    try:
        logger.log({3: _measurement()[3]})
        logger.log({3: _measurement()[3]})
        assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 3
    finally:
        logger.close()
