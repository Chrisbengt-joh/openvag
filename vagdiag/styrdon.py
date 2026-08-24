"""Styrdonsadresser och etiketter för mätvärdesblock.

Adresstabellen är generell för VAG-bilar från 1990-talet. Gruppetiketterna
gäller motorstyrdonet (adress 0x01) på 1.9 TDI med VP37-fördelarpump
(EDC15, motorkoder AHF/ALH/AFN/AGR m.fl.).

**Etiketterna är ungefärliga.** Blockinnehållet skiljer sig mellan
motorkoder och programvaruversioner. Använd dem som vägledning och jämför
mot VCDS eller ELSA för din exakta motorkod innan du drar slutsatser.
"""

from __future__ import annotations

__all__ = [
    "ADRESSER",
    "AUTOSCAN_ADRESSER",
    "KANSLIGA_ADRESSER",
    "styrdonsnamn",
    "GRUPPER_TDI_VP37",
    "etikett",
    "gruppnamn",
    "grupp_ar_kand",
]

#: Styrdonsadress -> svenskt namn.
ADRESSER: dict[int, str] = {
    0x01: "Motorstyrdon",
    0x02: "Automatväxellåda",
    0x03: "ABS-bromsar",
    0x04: "Rattvinkel",
    0x05: "Startspärr (Access/Start)",
    0x06: "Passagerarsäte",
    0x07: "Displayenhet",
    0x08: "Klimatanläggning / värme",
    0x09: "Centralelektronik",
    0x0F: "Digital radio",
    0x11: "Motorstyrdon II",
    0x15: "Airbag",
    0x16: "Rattelektronik",
    0x17: "Kombiinstrument",
    0x18: "Extravärmare",
    0x19: "Gateway (databuss)",
    0x1C: "Positionssensor",
    0x22: "Fyrhjulsdrift",
    0x25: "Immobilizer (startspärr)",
    0x35: "Centrallås",
    0x36: "Förarsäte",
    0x37: "Navigation",
    0x45: "Innerkomfort",
    0x46: "Komfortsystem (centralmodul)",
    0x47: "Ljudsystem",
    0x55: "Strålkastarinställning",
    0x56: "Radio",
    0x57: "TV-mottagare",
    0x76: "Parkeringshjälp",
    0x77: "Telefon",
}

#: Adresser som scannas vid auto-scan (i den ordning de provas).
AUTOSCAN_ADRESSER: tuple[int, ...] = (
    0x01, 0x02, 0x03, 0x08, 0x15, 0x16, 0x17, 0x19,
    0x25, 0x35, 0x36, 0x45, 0x46, 0x55, 0x56, 0x76,
)

#: Adresser där anpassning kan orsaka startspärr eller trasig mätarställning.
KANSLIGA_ADRESSER: frozenset[int] = frozenset({0x17, 0x25, 0x05})


def styrdonsnamn(adress: int) -> str:
    """Svenskt namn på styrdonet, eller en hexvisning om adressen är okänd."""
    return ADRESSER.get(adress, f"Okänt styrdon 0x{adress:02X}")


# ---------------------------------------------------------------------------
# Mätvärdesblock för 1.9 TDI VP37 (EDC15)
# ---------------------------------------------------------------------------

#: Gruppnummer -> (gruppnamn, fyra positionsetiketter).
GRUPPER_TDI_VP37: dict[int, tuple[str, tuple[str, str, str, str]]] = {
    1: (
        "Grundvärden motor",
        ("Varvtal", "Insprutad mängd", "Kylvätsketemperatur", "Motorbelastning"),
    ),
    2: (
        "Gaspedal och kontakter",
        ("Varvtal", "Insprutad mängd", "Gaspedalläge", "Kontaktstatus (bitfält)"),
    ),
    3: (
        "Luftmassa / EGR",
        ("Varvtal", "Luftmassa BÖR", "Luftmassa ÄR", "EGR-ventil styrgrad"),
    ),
    4: (
        "Insprutningsstart",
        ("Varvtal", "Insprutningsstart BÖR", "Insprutningsstart ÄR",
         "Styrgrad N108 insprutn.start"),
    ),
    5: (
        "Mängdställare",
        ("Varvtal", "Insprutad mängd", "Mängdställare ÄR", "Mängdställare status"),
    ),
    6: (
        "Temperaturer",
        ("Varvtal", "Insprutad mängd", "Kylvätsketemperatur", "Insugslufttemperatur"),
    ),
    7: (
        "Bränsletemperatur",
        ("Varvtal", "Insprutad mängd", "Bränsletemperatur", "Mängdkorrigering"),
    ),
    8: (
        "Matningsspänning",
        ("Varvtal", "Insprutad mängd", "Batterispänning", "Glödstiftsstatus"),
    ),
    9: (
        "Hastighet och last",
        ("Varvtal", "Insprutad mängd", "Hastighet", "Lastsignal"),
    ),
    10: (
        "Förvärmning",
        ("Varvtal", "Kylvätsketemperatur", "Glödtid", "Glödstiftsrelä"),
    ),
    11: (
        "Laddtrycksreglering",
        ("Varvtal", "Laddtryck BÖR", "Laddtryck ÄR", "Styrgrad N75 laddtryck"),
    ),
    12: (
        "Höjd och omgivning",
        ("Varvtal", "Atmosfärtryck", "Insugslufttemperatur", "Höjdkorrigering"),
    ),
    13: (
        "Jämngångsreglering",
        ("Cylinder 1 avvikelse", "Cylinder 2 avvikelse",
         "Cylinder 3 avvikelse", "Cylinder 4 avvikelse"),
    ),
    14: (
        "Startmängd",
        ("Varvtal", "Startmängd", "Kylvätsketemperatur", "Starttid"),
    ),
    15: (
        "Feltäljare",
        ("Antal fel", "Körsträcka sedan fel", "Feltid", "Reserverat"),
    ),
    16: (
        "Immobilizer / status",
        ("Varvtal", "Immobilizerstatus", "Reserverat", "Reserverat"),
    ),
    18: (
        "Kompressionsprov (grundinställning)",
        ("Cylinder 1", "Cylinder 2", "Cylinder 3", "Cylinder 4"),
    ),
}

_OKAND_GRUPP = ("Okänd grupp", ("Värde 1", "Värde 2", "Värde 3", "Värde 4"))


def gruppnamn(grupp: int, adress: int = 0x01) -> str:
    """Namn på mätvärdesblocket. Endast motorstyrdonet har etiketter i v1."""
    if adress != 0x01:
        return _OKAND_GRUPP[0]
    post = GRUPPER_TDI_VP37.get(grupp)
    return post[0] if post else _OKAND_GRUPP[0]


def etikett(grupp: int, position: int, adress: int = 0x01) -> str:
    """Etikett för ett värde. ``position`` är 1–4."""
    if adress != 0x01:
        return f"Värde {position}"
    post = GRUPPER_TDI_VP37.get(grupp)
    if not post or not 1 <= position <= 4:
        return f"Värde {position}"
    return post[1][position - 1]


def grupp_ar_kand(grupp: int, adress: int = 0x01) -> bool:
    """True om vi har etiketter för gruppen (annars visas generiska namn)."""
    return adress == 0x01 and grupp in GRUPPER_TDI_VP37
