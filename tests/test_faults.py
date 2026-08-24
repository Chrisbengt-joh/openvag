"""Tests for the fault-code database and the status-byte decoding."""

from __future__ import annotations

from vagdiag import faults
from vagdiag.faults import actuator_name, decode, decode_block, status_text


def test_decode_known_code():
    """17965 = 0x462D should decode to charge pressure, intermittent."""
    code = decode(0x46, 0x2D, 0xAA)
    assert code.code == 17965
    assert code.number == "17965"
    assert code.known is True
    assert "Charge pressure" in code.text
    assert code.intermittent is True


def test_decode_static_code():
    """Without bit 0x80 the fault is static."""
    code = decode(0x02, 0x30, 0x2A)
    assert code.code == 560
    assert code.number == "00560"
    assert code.intermittent is False
    assert "static" in code.status_text
    assert "recirculation" in code.text


def test_unknown_code_suggests_a_search():
    """An unknown code should show the number and prompt a web search."""
    code = decode(0x30, 0x39, 0x00)
    assert code.known is False
    assert "12345" in code.text
    assert "VAG fault code" in code.text


def test_status_text_keeps_the_raw_value():
    """The elaboration is a guess - the hex value must always survive."""
    text = status_text(0xAA)
    assert "intermittent" in text
    assert "0xAA" in text
    assert "control limit" in text


def test_status_text_unknown_elaboration():
    """An unknown elaboration must be omitted, not crash."""
    text = status_text(0x7F)
    assert "static" in text
    assert "0x7F" in text


def test_decode_block_with_several_codes():
    """A block holding three codes must yield three entries."""
    data = bytes([0x46, 0x2D, 0xAA, 0x02, 0x30, 0x2A, 0x00, 0x01, 0x00])
    assert [c.code for c in decode_block(data)] == [17965, 560, 1]


def test_decode_block_filters_empty_slots():
    """0xFFFF means "no fault" and must be filtered out."""
    assert decode_block(bytes([0xFF, 0xFF, 0x88])) == []
    codes = decode_block(bytes([0xFF, 0xFF, 0x88, 0x46, 0x2D, 0xAA]))
    assert [c.code for c in codes] == [17965]


def test_decode_block_ignores_a_truncated_tail():
    """A cut-off final triplet must be ignored rather than crash."""
    assert decode_block(bytes([0x46, 0x2D])) == []


def test_database_covers_the_tdi_key_codes():
    """Codes referenced by the troubleshooting guide must exist."""
    for code in (17965, 17966, 16485, 16486, 16487, 553, 560, 17811,
                 16785, 16786, 550, 1248, 1268, 1111, 65535):
        assert code in faults.FAULT_CODES, f"missing {code}"


def test_fault_code_str_is_readable():
    """The text form must carry the number, the description and the status."""
    text = str(decode(0x46, 0x2D, 0xAA))
    assert "17965" in text
    assert "[INT]" in text
    assert "0xAA" in text


def test_actuator_name():
    """Known component codes get a name, unknown ones show as hex."""
    assert "N18" in actuator_name(0x0102)
    assert "0x0999" in actuator_name(0x0999)
