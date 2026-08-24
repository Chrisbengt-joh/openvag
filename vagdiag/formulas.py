"""Measuring-value formulas for KWP1281.

A control module delivers each measuring value as three bytes:
``(formula_id, a, b)``. The formula id selects the conversion and the unit.
The table below is data driven - add or replace entries with :func:`register`.

**About reliability:** ids 1-21 and 25 are well documented and match VCDS in
practice. Ids 22 and up are reconstructed from public sources and log
comparisons; they are flagged ``verified=False`` and rendered with a trailing
``?`` in the user interface. Always compare a ``?`` value against VCDS before
drawing conclusions from it. An unknown id never crashes - it is shown as
``raw(id,a,b)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union

__all__ = [
    "Formula",
    "Reading",
    "FORMULAS",
    "compute",
    "register",
    "encode",
    "format_number",
]

#: A computed value is a number, a string (bit field, ASCII) or None.
Value = Union[float, str, None]


@dataclass(frozen=True)
class Formula:
    """One conversion formula for a measuring value."""

    id: int
    unit: str
    description: str
    function: Callable[[int, int], Value]
    verified: bool = True


@dataclass(frozen=True)
class Reading:
    """A fully converted measuring value."""

    formula_id: int
    a: int
    b: int
    value: Value
    unit: str
    verified: bool = True
    known: bool = True

    @property
    def is_number(self) -> bool:
        """True when the value is numeric (loggable and plottable)."""
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    @property
    def number(self) -> float | None:
        """The value as a float, or None when it is not numeric."""
        return float(self.value) if self.is_number else None  # type: ignore[arg-type]

    @property
    def text(self) -> str:
        """Formatted value with unit, for example ``2450 rpm``."""
        if not self.known or self.value is None:
            return f"raw({self.formula_id},{self.a},{self.b})"
        if isinstance(self.value, str):
            return self.value
        flag = "" if self.verified else " ?"
        unit = f" {self.unit}" if self.unit else ""
        return f"{format_number(float(self.value))}{unit}{flag}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def format_number(v: float) -> str:
    """Format a number with a sensible number of decimals for the terminal."""
    magnitude = abs(v)
    if magnitude >= 1000:
        return f"{v:.0f}"
    if magnitude >= 100:
        return f"{v:.1f}"
    if magnitude >= 10:
        return f"{v:.2f}"
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def _bit_field(a: int, b: int) -> str:
    """Id 16: eight binary flags in ``b``. ``a`` masks the active bits."""
    bits = f"{b:08b}"
    if a in (0, 0xFF):
        return bits
    return f"{bits} (mask {a:08b})"


def _ascii_pair(a: int, b: int) -> str:
    """Id 17: two ASCII characters."""
    return "".join(chr(x) if 32 <= x < 127 else "." for x in (a, b))


def _cold_warm(a: int, b: int) -> str:
    """Id 10: binary temperature flag."""
    return "warm" if b else "cold"


def _time_mm_ss(a: int, b: int) -> str:
    """Id 44: minutes:seconds."""
    return f"{a:d}:{b:02d}"


def _safe_div(numerator: float, denominator: float) -> float:
    """Division that yields 0 instead of raising on a zero denominator."""
    return numerator / denominator if denominator else 0.0


# ---------------------------------------------------------------------------
# The formula table
# ---------------------------------------------------------------------------

_TABLE: list[Formula] = [
    Formula(0, "", "unused", lambda a, b: None),
    Formula(1, "rpm", "engine speed", lambda a, b: 0.2 * a * b),
    Formula(2, "%", "percentage", lambda a, b: a * 0.002 * b),
    Formula(3, "deg", "angle", lambda a, b: 0.002 * a * b),
    Formula(4, "deg BTDC", "angle before top dead centre",
            lambda a, b: abs(b - 127) * 0.01 * a),
    Formula(5, "C", "temperature", lambda a, b: a * (b - 100) * 0.1),
    Formula(6, "V", "voltage", lambda a, b: 0.001 * a * b),
    Formula(7, "km/h", "speed", lambda a, b: 0.01 * a * b),
    Formula(8, "", "dimensionless", lambda a, b: 0.1 * a * b),
    Formula(9, "deg", "angle (signed)", lambda a, b: (b - 127) * 0.02 * a),
    Formula(10, "", "cold/warm", _cold_warm),
    Formula(11, "", "factor", lambda a, b: 0.0001 * a * (b - 128) + 1),
    Formula(12, "ohm", "resistance", lambda a, b: 0.001 * a * b),
    Formula(13, "mm", "length (signed)", lambda a, b: (b - 127) * 0.001 * a),
    Formula(14, "bar", "pressure", lambda a, b: 0.005 * a * b),
    Formula(15, "ms", "time", lambda a, b: 0.01 * a * b),
    Formula(16, "", "bit field", _bit_field),
    Formula(17, "", "ASCII pair", _ascii_pair),
    Formula(18, "mbar", "pressure", lambda a, b: 0.04 * a * b),
    Formula(19, "l", "volume", lambda a, b: a * b * 0.01),
    Formula(20, "%", "percentage (signed)", lambda a, b: a * (b - 128) / 128.0),
    Formula(21, "V", "voltage", lambda a, b: 0.001 * a * b),
    # -- from here down: reconstructed, compare against VCDS ------------------
    Formula(22, "", "ratio", lambda a, b: _safe_div(a * 2000.0, b), verified=False),
    Formula(23, "%", "percentage", lambda a, b: b / 256.0 * a, verified=False),
    Formula(24, "A", "current", lambda a, b: a * b / 32768.0, verified=False),
    Formula(25, "g/s", "air mass", lambda a, b: (b * 1.421) + (a / 182.0)),
    Formula(26, "C", "temperature difference", lambda a, b: float(b - a), verified=False),
    Formula(27, "mg/stroke", "injected quantity (signed)",
            lambda a, b: abs(b - 128) * 0.01 * a, verified=False),
    Formula(28, "", "difference", lambda a, b: float(b - a), verified=False),
    Formula(29, "", "absolute difference", lambda a, b: float(abs(b - a)), verified=False),
    Formula(30, "deg/crank", "angle per crank revolution",
            lambda a, b: b / 12.0 * a, verified=False),
    Formula(31, "C", "temperature", lambda a, b: b / 2560.0 * a, verified=False),
    Formula(32, "", "signed value", lambda a, b: float(b - 256 if b > 128 else b),
            verified=False),
    Formula(33, "%", "percentage", lambda a, b: _safe_div(100.0 * a, b), verified=False),
    Formula(34, "kW", "power", lambda a, b: (b - 128) * 0.01 * a, verified=False),
    Formula(35, "l/h", "flow", lambda a, b: 0.01 * a * b, verified=False),
    Formula(36, "km", "distance", lambda a, b: float(a * 2560 + b * 10), verified=False),
    Formula(37, "s", "time", lambda a, b: float(a * 256 + b) * 0.01, verified=False),
    Formula(38, "deg/crank", "angle (signed)", lambda a, b: (b - 128) * 0.001 * a,
            verified=False),
    Formula(39, "mg/stroke", "quantity deviation (signed)",
            lambda a, b: (b - 128) * 0.1 * a, verified=False),
    Formula(40, "A", "current", lambda a, b: b * 0.1 + (25.5 * a) - 400.0, verified=False),
    Formula(41, "Ah", "charge", lambda a, b: float(b + a * 255), verified=False),
    Formula(42, "kW", "power", lambda a, b: b * 0.1 + (25.5 * a) - 400.0, verified=False),
    Formula(43, "V", "voltage", lambda a, b: b * 0.1 + (25.5 * a), verified=False),
    Formula(44, "", "time mm:ss", _time_mm_ss, verified=False),
    Formula(45, "", "percentage", lambda a, b: 0.1 * a * b / 100.0, verified=False),
    Formula(46, "mbar", "pressure difference", lambda a, b: (a * b - 3200) * 0.0027,
            verified=False),
    Formula(47, "ms", "time (signed)", lambda a, b: float((b - 128) * a), verified=False),
    Formula(48, "", "raw 16-bit", lambda a, b: float(b + a * 255), verified=False),
    Formula(49, "mg/stroke", "injected quantity", lambda a, b: (b / 4.0) * a * 0.1,
            verified=False),
    Formula(50, "mbar", "pressure (signed)",
            lambda a, b: _safe_div(a * (b - 128), 128.0), verified=False),
    Formula(51, "mg/stroke", "air mass / quantity", lambda a, b: 0.1 * a * b,
            verified=False),
    Formula(52, "Nm", "torque", lambda a, b: b * 0.02 * a - a, verified=False),
    Formula(53, "g/s", "air mass (signed)",
            lambda a, b: (b - 128) * 1.4222 + 0.006 * a, verified=False),
    Formula(54, "", "counter", lambda a, b: float(a * 256 + b), verified=False),
]

#: Lookup table: formula id -> :class:`Formula`.
FORMULAS: dict[int, Formula] = {f.id: f for f in _TABLE}


def register(formula: Formula) -> None:
    """Add or replace a formula (after comparing against VCDS, for example)."""
    FORMULAS[formula.id] = formula


def compute(formula_id: int, a: int, b: int) -> Reading:
    """Convert ``(formula_id, a, b)`` into a :class:`Reading`.

    Never raises - an unknown id or a broken formula yields a raw value instead.
    """
    formula = FORMULAS.get(formula_id)
    if formula is None:
        return Reading(formula_id, a, b, None, "", verified=False, known=False)
    try:
        value = formula.function(a, b)
    except Exception:
        return Reading(formula_id, a, b, None, formula.unit, verified=False, known=False)
    return Reading(formula_id, a, b, value, formula.unit, formula.verified)


def encode(formula_id: int, wanted: float, a: int = 100) -> tuple[int, int, int]:
    """Inverse of :func:`compute` - find ``(formula_id, a, b)`` for a value.

    Used by the ECU simulator to serve realistic measuring values. ``b`` is
    searched exhaustively (0-255) for the given ``a`` and the best match wins.
    """
    formula = FORMULAS.get(formula_id)
    a &= 0xFF
    if formula is None:
        return (formula_id & 0xFF, a, 0)
    best_b = 0
    best_error = float("inf")
    for b in range(256):
        try:
            v = formula.function(a, b)
        except Exception:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        error = abs(float(v) - wanted)
        if error < best_error:
            best_error, best_b = error, b
            if error == 0.0:
                break
    return (formula_id & 0xFF, a, best_b)
