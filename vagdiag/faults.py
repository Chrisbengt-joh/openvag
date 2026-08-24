"""Fault-code database and status-byte decoding for KWP1281.

A fault block (title 0xFC) carries three bytes per code:
``(high, low, status)``. The code is the 16-bit value ``high<<8 | low``, which
VAG displays as a five-digit decimal number, for example 17965.

The status byte describes *how* the fault looks. Bit 0x80 means intermittent
and is well documented. The rest - the elaboration code - is a best-effort
interpretation, so the raw hex value is always displayed alongside it for
comparison against VCDS.

The database covers the most common VAG codes with an emphasis on the 1.9 TDI
(VP37/EDC15). An unknown code prints a prompt to search for it online.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FaultCode",
    "FAULT_CODES",
    "decode",
    "decode_block",
    "description",
    "status_text",
    "ACTUATORS",
    "actuator_name",
]

#: Code the control module sends when the fault memory is empty.
NO_FAULT = 0xFFFF

#: Status-byte bit marking an intermittent (non-permanent) fault.
BIT_INTERMITTENT = 0x80


# ---------------------------------------------------------------------------
# Elaborations (best-effort interpretation - the raw value is always shown)
# ---------------------------------------------------------------------------

_ELABORATION: dict[int, str] = {
    0x00: "no further information",
    0x10: "signal out of tolerance",
    0x11: "signal too high / above upper limit",
    0x12: "signal too low / below lower limit",
    0x13: "no signal / open circuit",
    0x14: "short to positive",
    0x15: "short to ground",
    0x16: "open circuit or short to positive",
    0x17: "open circuit or short to ground",
    0x18: "implausible signal",
    0x19: "mechanical fault",
    0x1A: "control limit exceeded",
    0x1B: "control limit undershot",
    0x20: "signal out of tolerance",
    0x21: "signal too high / above upper limit",
    0x22: "signal too low / below lower limit",
    0x23: "no signal / open circuit",
    0x24: "open circuit or short to positive",
    0x25: "open circuit or short to ground",
    0x26: "short to positive",
    0x27: "short to ground",
    0x28: "mechanical fault",
    0x29: "implausible signal",
    0x2A: "control limit exceeded",
    0x2B: "no communication",
    0x2C: "incorrect operation",
    0x2D: "open circuit",
    0x2E: "short circuit",
    0x2F: "basic setting not carried out",
    0x30: "control limit undershot",
    0x35: "basic setting missing or incorrect",
    0x3C: "control module defective",
}


def status_text(status: int) -> str:
    """Decode the status byte into plain text.

    Bit 0x80 (intermittent) is reliable; the elaboration is an approximation,
    which is why the hex value is always included.
    """
    parts: list[str] = [
        "intermittent" if status & BIT_INTERMITTENT else "static"
    ]
    elaboration = status & 0x7F
    text = _ELABORATION.get(elaboration)
    if text:
        parts.append(text)
    parts.append(f"status 0x{status:02X}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Fault-code database
# ---------------------------------------------------------------------------

#: Five-digit VAG code -> plain-text description.
FAULT_CODES: dict[int, str] = {
    # -- General / control module ------------------------------------------
    65535: "Internal control module fault - module defective",
    65534: "Internal control module fault - memory error",
    1111: "Engine control module defective (internal fault)",
    1044: "Control module incorrectly coded / coding invalid",
    1087: "Basic setting not carried out",
    1119: "Gearbox coding missing or incorrect",
    1120: "Calibration not carried out",
    1177: "No signal from engine control module",
    1182: "Altitude adaptation - limit reached",
    1312: "Drivetrain data bus - malfunction",
    1314: "Engine control module - no communication",
    1316: "ABS control module - no communication",
    1317: "Instrument cluster - no communication",
    1321: "Airbag control module - no communication",
    1324: "Four-wheel-drive control module - no communication",
    # -- Sensors and supply ------------------------------------------------
    513: "Engine speed sensor G28 - faulty signal",
    514: "Crankshaft position sensor G28 - no or faulty signal",
    515: "Hall sender G40 - faulty signal",
    516: "Idle switch F60 - faulty signal",
    518: "Throttle position sensor G69 - faulty signal",
    519: "Charge pressure sensor G31 - faulty signal",
    520: "Mass air flow sensor G70 - faulty signal",
    522: "Coolant temperature sensor G62 - faulty signal",
    523: "Intake air temperature sensor G72 - faulty signal",
    524: "Knock sensor 1 G61 - faulty signal",
    525: "Oxygen sensor G39 - faulty signal",
    530: "Accelerator pedal position sensor G79 - faulty signal",
    532: "Supply voltage - out of tolerance",
    533: "Idle speed control - control limit reached",
    537: "Oxygen sensor control - control limit reached",
    543: "Maximum engine speed exceeded",
    549: "Fuel consumption signal - faulty",
    550: "Start of injection control - control limit reached",
    553: "Mass air flow sensor G70 - signal out of tolerance",
    555: "Altitude sensor F96 - faulty signal",
    558: "Quantity adjuster - control limit reached",
    560: "Exhaust gas recirculation - control limit reached / insufficient flow",
    561: "Mixture control - adaptation limit reached",
    575: "Intake manifold pressure / charge pressure - control limit reached",
    577: "Cylinder 1 - knock control at limit",
    609: "Ignition amplifier - malfunction",
    625: "Vehicle speed signal - faulty",
    668: "Supply voltage terminal 30 - out of tolerance",
    281: "Vehicle speed sensor G22 - faulty signal",
    283: "ABS wheel speed sensor - faulty signal",
    778: "Steering angle sensor G85 - faulty signal",
    779: "Ambient temperature sensor G17 - faulty signal",
    # -- Actuators (VP37/EDC15) --------------------------------------------
    1247: "Cold start valve - electrical fault in circuit",
    1248: "Start of injection valve N108 - electrical fault in circuit",
    1249: "Injector cylinder 1 N30 - electrical fault in circuit",
    1250: "Injector cylinder 2 N31 - electrical fault in circuit",
    1251: "Injector cylinder 3 N32 - electrical fault in circuit",
    1252: "Injector cylinder 4 N33 - electrical fault in circuit",
    1253: "EGR valve N18 - electrical fault in circuit",
    1257: "Idle stabilisation - malfunction",
    1259: "Fuel pump relay J17 - electrical fault in circuit",
    1262: "Charge pressure control valve N75 - electrical fault in circuit",
    1265: "EGR valve N18 - electrical fault in circuit",
    1266: "Glow plug relay J52 - electrical fault in circuit",
    1268: "Quantity adjuster N146 - electrical fault in circuit",
    1269: "Fuel shut-off valve N109 - electrical fault in circuit",
    1271: "Radiator fan - electrical fault in circuit",
    1274: "Engine control module relay - electrical fault in circuit",
    # -- P-code range (16384+) ---------------------------------------------
    16485: "Mass air flow sensor G70 - signal out of tolerance (P0101)",
    16486: "Mass air flow sensor G70 - signal too low (P0102)",
    16487: "Mass air flow sensor G70 - signal too high (P0103)",
    16496: "Intake air temperature sensor G42 - signal too low",
    16497: "Intake air temperature sensor G42 - signal too high",
    16500: "Coolant temperature sensor G62 - signal out of tolerance",
    16501: "Coolant temperature sensor G62 - signal too low",
    16502: "Coolant temperature sensor G62 - signal too high",
    16505: "Fuel temperature sensor G81 - signal out of tolerance",
    16506: "Fuel temperature sensor G81 - signal too low",
    16507: "Fuel temperature sensor G81 - signal too high",
    16514: "Oxygen sensor G39 - circuit malfunction",
    16554: "Cylinder 1 - injector circuit malfunction",
    16555: "Cylinder 2 - injector circuit malfunction",
    16556: "Cylinder 3 - injector circuit malfunction",
    16557: "Cylinder 4 - injector circuit malfunction",
    16684: "Random misfire detected (P0300)",
    16685: "Cylinder 1 - misfire detected",
    16686: "Cylinder 2 - misfire detected",
    16687: "Cylinder 3 - misfire detected",
    16688: "Cylinder 4 - misfire detected",
    16705: "Engine speed sensor G28 - faulty signal",
    16706: "Engine speed sensor G28 - no signal",
    16785: "Exhaust gas recirculation - insufficient flow (P0401)",
    16786: "Exhaust gas recirculation - excessive flow (P0402)",
    16787: "EGR valve N18 - electrical fault in circuit (P0403)",
    16792: "EGR temperature sensor - out of tolerance",
    16825: "Evaporative emission canister purge valve N80 - electrical fault",
    16989: "Idle control - engine speed out of tolerance",
    17091: "Intake air temperature too high - power reduction active",
    17509: "Oxygen sensor heater circuit malfunction",
    17544: "Fuel mixture too rich (bank 1)",
    17545: "Fuel mixture too lean (bank 1)",
    17604: "Start of injection valve N108 - short to positive",
    17605: "Start of injection valve N108 - short to ground",
    17606: "Start of injection valve N108 - open circuit",
    17610: "Quantity adjuster N146 - control limit reached",
    17638: "Engine control module - insufficient self-diagnosis",
    17800: "Brake light switch F - implausible signal",
    17811: "Exhaust gas recirculation - control deviation (P1403)",
    17815: "Charge pressure sensor G31 - implausible signal",
    17816: "Charge pressure sensor G31 - signal too low",
    17817: "Charge pressure sensor G31 - signal too high",
    17910: "Start of injection - control deviation",
    17911: "Start of injection - control limit undershot",
    17912: "Start of injection - control limit exceeded",
    17963: "Charge pressure control - control limit not reached",
    17964: "Charge pressure control - negative deviation (P1556)",
    17965: "Charge pressure control - positive deviation (P1557)",
    17966: "Throttle actuator - electrical fault in circuit (P1558)",
    17967: "Throttle position - adaptation malfunction",
    17978: "Immobilizer - no authorisation / vehicle locked",
    17979: "Immobilizer - no signal from control module",
    18010: "Supply voltage terminal 30 - too low (P1602)",
    18011: "Supply voltage - too high",
    18013: "Altitude sensor - implausible signal",
    18034: "Engine control module - supply voltage too low",
    18265: "Load signal from engine control module - faulty",
    19289: "Glow plug relay J52 - electrical fault in circuit",
    # -- Instrument cluster / other ----------------------------------------
    1039: "Instrument cluster illumination - electrical fault in circuit",
    1041: "Warning buzzer - electrical fault in circuit",
    1122: "Fuel level sender G - faulty signal",
    2020: "Malfunction indicator lamp (MIL) - electrical fault / bulb blown",
}


def description(code: int) -> tuple[str, bool]:
    """Return (text, known). An unknown code yields a search prompt."""
    text = FAULT_CODES.get(code)
    if text:
        return text, True
    return (
        f"Unknown fault code - search the web for \"VAG fault code {code:05d}\"",
        False,
    )


@dataclass(frozen=True)
class FaultCode:
    """One decoded fault code from the control module's memory."""

    code: int
    status: int
    text: str
    known: bool = True

    @property
    def number(self) -> str:
        """Five-digit VAG number as text, for example ``17965``."""
        return f"{self.code:05d}"

    @property
    def intermittent(self) -> bool:
        """True when the fault is intermittent (bit 0x80 in the status byte)."""
        return bool(self.status & BIT_INTERMITTENT)

    @property
    def status_text(self) -> str:
        """The status byte in plain text."""
        return status_text(self.status)

    def __str__(self) -> str:
        marker = " [INT]" if self.intermittent else ""
        return f"{self.number}{marker} - {self.text}\n        ({self.status_text})"


def decode(high: int, low: int, status: int) -> FaultCode:
    """Decode a fault code from three raw bytes."""
    code = ((high & 0xFF) << 8) | (low & 0xFF)
    text, known = description(code)
    return FaultCode(code, status & 0xFF, text, known)


def decode_block(data: bytes) -> list[FaultCode]:
    """Decode a whole 0xFC block. Empty slots (0xFFFF) are filtered out."""
    codes: list[FaultCode] = []
    for i in range(0, len(data) - 2, 3):
        high, low, status = data[i], data[i + 1], data[i + 2]
        if ((high << 8) | low) == NO_FAULT:
            continue
        codes.append(decode(high, low, status))
    return codes


# ---------------------------------------------------------------------------
# Actuators (component codes in 0xF5 responses)
# ---------------------------------------------------------------------------

#: Component code -> name. Approximate table; the raw value is always shown.
ACTUATORS: dict[int, str] = {
    0x0000: "Actuator test finished",
    0x0101: "Start of injection valve N108",
    0x0102: "EGR valve N18",
    0x0103: "Charge pressure control valve N75",
    0x0104: "Intake manifold flap / throttle actuator",
    0x0105: "Glow plug relay J52",
    0x0106: "Fuel pump relay J17",
    0x0107: "Quantity adjuster N146",
    0x0108: "Fuel shut-off valve N109",
    0x0109: "Radiator fan, low speed",
    0x010A: "Radiator fan, high speed",
    0x010B: "Air conditioning compressor clutch",
    0x010C: "Malfunction indicator lamp (MIL) K83",
    0x010D: "Glow plug indicator lamp K29",
    0x010E: "Evaporative emission canister purge valve N80",
}


def actuator_name(code: int) -> str:
    """Name of an actuator from its component code. Unknown codes show as hex."""
    name = ACTUATORS.get(code)
    if name:
        return name
    return f"Unknown actuator (component code 0x{code:04X} / {code})"
