"""Tests for the measuring-value formulas."""

from __future__ import annotations

import pytest

from vagdiag import formulas
from vagdiag.formulas import compute, encode


@pytest.mark.parametrize(
    "formula_id, a, b, expected, unit",
    [
        (1, 100, 50, 1000.0, "rpm"),        # 0.2 * a * b
        (2, 200, 100, 40.0, "%"),           # a * 0.002 * b
        (3, 100, 50, 10.0, "deg"),          # 0.002 * a * b
        (4, 20, 137, 2.0, "deg BTDC"),      # abs(b-127) * 0.01 * a
        (5, 10, 200, 100.0, "C"),           # a * (b-100) * 0.1
        (6, 100, 138, 13.8, "V"),           # 0.001 * a * b
        (7, 100, 90, 90.0, "km/h"),         # 0.01 * a * b
        (9, 50, 137, 10.0, "deg"),          # (b-127) * 0.02 * a
        (12, 100, 200, 20.0, "ohm"),        # 0.001 * a * b
        (14, 100, 20, 10.0, "bar"),         # 0.005 * a * b
        (15, 100, 25, 25.0, "ms"),          # 0.01 * a * b
        (18, 200, 100, 800.0, "mbar"),      # 0.04 * a * b
        (20, 128, 192, 64.0, "%"),          # a * (b-128) / 128
        (25, 182, 10, 15.21, "g/s"),        # (b*1.421) + (a/182)
    ],
)
def test_known_formulas(formula_id, a, b, expected, unit):
    """Known inputs must produce known outputs."""
    reading = compute(formula_id, a, b)
    assert reading.number == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert reading.unit == unit
    assert reading.known


def test_cold_warm():
    """Id 10 is a binary temperature flag."""
    assert compute(10, 0, 0).value == "cold"
    assert compute(10, 0, 1).value == "warm"


def test_bit_field():
    """Id 16 shows eight binary flags."""
    assert compute(16, 0xFF, 0b10100101).value == "10100101"
    assert "mask" in str(compute(16, 0b00001111, 0b10100101).value)


def test_ascii_pair():
    """Id 17 is two ASCII characters; unprintable bytes become dots."""
    assert compute(17, 65, 66).value == "AB"
    assert compute(17, 0, 66).value == ".B"


def test_unknown_formula_gives_raw_value():
    """An unknown formula id must never crash, only render raw."""
    reading = compute(250, 1, 2)
    assert reading.known is False
    assert reading.number is None
    assert reading.text == "raw(250,1,2)"


def test_no_formula_ever_crashes():
    """Every id 0-255 with extreme inputs must pass without raising."""
    for formula_id in range(256):
        for a in (0, 1, 127, 128, 255):
            for b in (0, 1, 127, 128, 255):
                reading = compute(formula_id, a, b)
                assert reading.text  # there is always something to display


def test_unverified_formulas_are_flagged():
    """Reconstructed formulas must be marked with a ? in their text."""
    assert compute(1, 100, 50).verified is True
    assert compute(39, 20, 138).verified is False
    assert compute(39, 20, 138).text.endswith("?")


def test_division_by_zero_gives_zero():
    """Formulas with a denominator must yield 0 rather than raising."""
    assert compute(22, 100, 0).number == 0.0
    assert compute(33, 100, 0).number == 0.0


def test_encode_is_the_inverse_of_compute():
    """The simulator's encoding must round-trip close to the wanted value."""
    for formula_id, wanted, a, tolerance in [
        (1, 2000.0, 100, 20.0),
        (5, 85.0, 10, 1.0),
        (18, 1800.0, 200, 10.0),
        (51, 640.0, 40, 5.0),
        (2, 45.0, 200, 1.0),
    ]:
        triplet = encode(formula_id, wanted, a=a)
        assert triplet[0] == formula_id
        assert 0 <= triplet[1] <= 255 and 0 <= triplet[2] <= 255
        assert compute(*triplet).number == pytest.approx(wanted, abs=tolerance)


def test_encode_unknown_id_does_not_crash():
    """Encoding an unknown id must give a valid but zeroed triplet."""
    assert encode(250, 100.0) == (250, 100, 0)


def test_text_formatting():
    """The text must carry the unit and a sensible number of decimals."""
    assert compute(1, 100, 50).text == "1000 rpm"
    assert compute(6, 100, 138).text == "13.80 V"


def test_register_a_custom_formula():
    """The table must be patchable after comparing against VCDS."""
    original = formulas.FORMULAS[51]
    try:
        formulas.register(
            formulas.Formula(51, "mg/stroke", "custom", lambda a, b: float(a + b))
        )
        assert compute(51, 10, 5).number == 15.0
    finally:
        formulas.register(original)
    assert compute(51, 10, 5).number == pytest.approx(5.0)
