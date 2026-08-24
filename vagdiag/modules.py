"""Control module addresses and measuring-block labels.

The address table is generic for 1990s VAG vehicles. The group labels apply to
the engine control module (address 0x01) on a 1.9 TDI with a VP37 injection
pump (EDC15, engine codes AHF/ALH/AFN/AGR and similar).

**The labels are approximate.** Block contents differ between engine codes and
software versions. Treat them as guidance and verify against VCDS or ELSA for
your exact engine code before drawing conclusions.
"""

from __future__ import annotations

__all__ = [
    "ADDRESSES",
    "AUTOSCAN_ADDRESSES",
    "SENSITIVE_ADDRESSES",
    "module_name",
    "GROUPS_TDI_VP37",
    "label",
    "group_name",
    "group_is_known",
]

#: Control module address -> name.
ADDRESSES: dict[int, str] = {
    0x01: "Engine",
    0x02: "Automatic transmission",
    0x03: "ABS brakes",
    0x04: "Steering angle",
    0x05: "Access/start authorisation",
    0x06: "Passenger seat",
    0x07: "Display unit",
    0x08: "Air conditioning / heating",
    0x09: "Central electrics",
    0x0F: "Digital radio",
    0x11: "Engine II",
    0x15: "Airbag",
    0x16: "Steering wheel electronics",
    0x17: "Instrument cluster",
    0x18: "Auxiliary heater",
    0x19: "Gateway (data bus)",
    0x1C: "Position sensing",
    0x22: "All-wheel drive",
    0x25: "Immobilizer",
    0x35: "Central locking",
    0x36: "Driver seat",
    0x37: "Navigation",
    0x45: "Interior monitoring",
    0x46: "Comfort system (central module)",
    0x47: "Sound system",
    0x55: "Headlight range adjustment",
    0x56: "Radio",
    0x57: "TV tuner",
    0x76: "Parking aid",
    0x77: "Telephone",
}

#: Addresses probed during an auto-scan, in the order they are tried.
AUTOSCAN_ADDRESSES: tuple[int, ...] = (
    0x01, 0x02, 0x03, 0x08, 0x15, 0x16, 0x17, 0x19,
    0x25, 0x35, 0x36, 0x45, 0x46, 0x55, 0x56, 0x76,
)

#: Addresses where adaptation can immobilise the car or corrupt the odometer.
SENSITIVE_ADDRESSES: frozenset[int] = frozenset({0x17, 0x25, 0x05})


def module_name(address: int) -> str:
    """Name of the control module, or a hex rendering for unknown addresses."""
    return ADDRESSES.get(address, f"Unknown module 0x{address:02X}")


# ---------------------------------------------------------------------------
# Measuring blocks for the 1.9 TDI VP37 (EDC15)
# ---------------------------------------------------------------------------

#: Group number -> (group name, four position labels).
GROUPS_TDI_VP37: dict[int, tuple[str, tuple[str, str, str, str]]] = {
    1: (
        "Engine basics",
        ("Engine speed", "Injected quantity", "Coolant temperature", "Engine load"),
    ),
    2: (
        "Accelerator and switches",
        ("Engine speed", "Injected quantity", "Accelerator position",
         "Switch status (bit field)"),
    ),
    3: (
        "Air mass / EGR",
        ("Engine speed", "Air mass SPEC", "Air mass ACTUAL", "EGR valve duty cycle"),
    ),
    4: (
        "Start of injection",
        ("Engine speed", "Injection start SPEC", "Injection start ACTUAL",
         "N108 duty cycle"),
    ),
    5: (
        "Quantity adjuster",
        ("Engine speed", "Injected quantity", "Quantity adjuster ACTUAL",
         "Quantity adjuster status"),
    ),
    6: (
        "Temperatures",
        ("Engine speed", "Injected quantity", "Coolant temperature",
         "Intake air temperature"),
    ),
    7: (
        "Fuel temperature",
        ("Engine speed", "Injected quantity", "Fuel temperature",
         "Quantity correction"),
    ),
    8: (
        "Supply voltage",
        ("Engine speed", "Injected quantity", "Battery voltage", "Glow plug status"),
    ),
    9: (
        "Speed and load",
        ("Engine speed", "Injected quantity", "Vehicle speed", "Load signal"),
    ),
    10: (
        "Preheating",
        ("Engine speed", "Coolant temperature", "Glow time", "Glow plug relay"),
    ),
    11: (
        "Charge pressure control",
        ("Engine speed", "Charge pressure SPEC", "Charge pressure ACTUAL",
         "N75 duty cycle"),
    ),
    12: (
        "Altitude and ambient",
        ("Engine speed", "Atmospheric pressure", "Intake air temperature",
         "Altitude correction"),
    ),
    13: (
        "Smooth running control",
        ("Cylinder 1 deviation", "Cylinder 2 deviation",
         "Cylinder 3 deviation", "Cylinder 4 deviation"),
    ),
    14: (
        "Starting quantity",
        ("Engine speed", "Starting quantity", "Coolant temperature", "Start time"),
    ),
    15: (
        "Fault counters",
        ("Fault count", "Distance since fault", "Fault time", "Reserved"),
    ),
    16: (
        "Immobilizer / status",
        ("Engine speed", "Immobilizer status", "Reserved", "Reserved"),
    ),
    18: (
        "Compression test (basic setting)",
        ("Cylinder 1", "Cylinder 2", "Cylinder 3", "Cylinder 4"),
    ),
}

_UNKNOWN_GROUP = ("Unknown group", ("Value 1", "Value 2", "Value 3", "Value 4"))


def group_name(group: int, address: int = 0x01) -> str:
    """Name of the measuring block. Only the engine module has labels in v1."""
    if address != 0x01:
        return _UNKNOWN_GROUP[0]
    entry = GROUPS_TDI_VP37.get(group)
    return entry[0] if entry else _UNKNOWN_GROUP[0]


def label(group: int, position: int, address: int = 0x01) -> str:
    """Label for one value. ``position`` is 1-4."""
    if address != 0x01:
        return f"Value {position}"
    entry = GROUPS_TDI_VP37.get(group)
    if not entry or not 1 <= position <= 4:
        return f"Value {position}"
    return entry[1][position - 1]


def group_is_known(group: int, address: int = 0x01) -> bool:
    """True when labels exist for the group (otherwise generic names are used)."""
    return address == 0x01 and group in GROUPS_TDI_VP37
