"""Felkodsdatabas och avkodning av statusbyte för KWP1281.

Ett felkodsblock (titel 0xFC) innehåller tre bytes per kod:
``(hög, låg, status)``. Koden är det 16-bitars talet ``hög<<8 | låg`` som VAG
visar som ett femsiffrigt decimaltal, t.ex. 17965.

Statusbyten säger *hur* felet ser ut. Bit 0x80 betyder sporadiskt fel och är
väl dokumenterad. Resten (elaborationskoden) tolkas efter bästa förmåga –
den råa hexvärdet visas alltid så att du kan jämföra mot VCDS.

Databasen täcker de vanligaste VAG-koderna med tyngdpunkt på 1.9 TDI
(VP37/EDC15). Okänd kod ger en uppmaning att googla.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Felkod",
    "FELKODER",
    "avkoda",
    "avkoda_block",
    "beskrivning",
    "statustext",
    "STALLDON",
    "stalldonsnamn",
]

#: Kod som styrdonet skickar när minnet är tomt.
INGEN_FELKOD = 0xFFFF

#: Bit i statusbyten som markerar sporadiskt (ej permanent) fel.
BIT_SPORADISK = 0x80


# ---------------------------------------------------------------------------
# Elaborationer (ungefärlig tolkning – råvärdet visas alltid)
# ---------------------------------------------------------------------------

_ELABORATION: dict[int, str] = {
    0x00: "inga ytterligare uppgifter",
    0x10: "signal utanför tolerans",
    0x11: "signal för hög / över övre gräns",
    0x12: "signal för låg / under undre gräns",
    0x13: "ingen signal / avbrott",
    0x14: "kortslutning till plus",
    0x15: "kortslutning till jord",
    0x16: "avbrott eller kortslutning till plus",
    0x17: "avbrott eller kortslutning till jord",
    0x18: "implausibel signal",
    0x19: "mekaniskt fel",
    0x1A: "reglergräns överskriden",
    0x1B: "reglergräns underskriden",
    0x20: "signal utanför tolerans",
    0x21: "signal för hög / över övre gräns",
    0x22: "signal för låg / under undre gräns",
    0x23: "ingen signal / avbrott",
    0x24: "avbrott eller kortslutning till plus",
    0x25: "avbrott eller kortslutning till jord",
    0x26: "kortslutning till plus",
    0x27: "kortslutning till jord",
    0x28: "mekaniskt fel",
    0x29: "implausibel signal",
    0x2A: "reglergräns överskriden",
    0x2B: "ingen kommunikation",
    0x2C: "felaktig funktion",
    0x2D: "avbrott",
    0x2E: "kortslutning",
    0x2F: "grundinställning ej utförd",
    0x30: "reglergräns underskriden",
    0x35: "ingen eller felaktig grundinställning",
    0x3C: "styrdon defekt",
}


def statustext(status: int) -> str:
    """Avkoda statusbyten till svensk klartext.

    Bit 0x80 = sporadisk är säker; elaborationen är en ungefärlig tolkning och
    hexvärdet visas därför alltid.
    """
    delar: list[str] = ["sporadisk" if status & BIT_SPORADISK else "statisk"]
    elab = status & 0x7F
    text = _ELABORATION.get(elab)
    if text:
        delar.append(text)
    delar.append(f"status 0x{status:02X}")
    return ", ".join(delar)


# ---------------------------------------------------------------------------
# Felkodsdatabas
# ---------------------------------------------------------------------------

#: Femsiffrig VAG-kod -> svensk klartext.
FELKODER: dict[int, str] = {
    # -- Generella / styrdon ------------------------------------------------
    65535: "Internt styrdonsfel – kontrollenhet defekt",
    65534: "Internt styrdonsfel – minnesfel",
    1111: "Motorstyrdon defekt (internt fel)",
    1044: "Styrdonet felkodat / kodning ogiltig",
    1087: "Grundinställning ej utförd",
    1119: "Växellådskodning saknas eller felaktig",
    1120: "Kalibrering ej utförd",
    1177: "Signal från motorstyrdon saknas",
    1182: "Höjdanpassning – gräns nådd",
    1312: "Databuss drivsystem – felfunktion",
    1314: "Motorstyrdon – ingen kommunikation",
    1316: "ABS-styrdon – ingen kommunikation",
    1317: "Kombiinstrument – ingen kommunikation",
    1321: "Airbagstyrdon – ingen kommunikation",
    1324: "Fyrhjulsdriftsstyrdon – ingen kommunikation",
    # -- Givare och matning -------------------------------------------------
    513: "Motorvarvtalsgivare G28 – felaktig signal",
    514: "Vevaxelgivare G28 – ingen eller felaktig signal",
    515: "Hallgivare G40 – felaktig signal",
    516: "Tomgångskontakt F60 – felaktig signal",
    518: "Gasspjällpotentiometer G69 – felaktig signal",
    519: "Laddtrycksgivare G31 – felaktig signal",
    520: "Luftmassemätare G70 – felaktig signal",
    522: "Kylvätsketemperaturgivare G62 – felaktig signal",
    523: "Insugslufttemperaturgivare G72 – felaktig signal",
    524: "Knacksensor 1 G61 – felaktig signal",
    525: "Lambdasond G39 – felaktig signal",
    530: "Gaspedalpotentiometer G79 – felaktig signal",
    532: "Matningsspänning – utanför tolerans",
    533: "Tomgångsvarvtalsreglering – reglergräns nådd",
    537: "Lambdareglering – reglergräns nådd",
    543: "Maximalt motorvarvtal överskridet",
    549: "Bränsleförbrukningssignal – felaktig",
    550: "Insprutningsstartreglering – reglergräns nådd",
    553: "Luftmassemätare G70 – signal utanför tolerans",
    555: "Höjdgivare F96 – felaktig signal",
    558: "Mängdställare – reglergräns nådd",
    560: "Avgasåterföring EGR – reglergräns nådd / otillräckligt flöde",
    561: "Blandningsreglering – adaptionsgräns nådd",
    575: "Insugsrörstryck / laddtryck – reglergräns nådd",
    577: "Cylinder 1 – knackreglering vid gräns",
    609: "Tändningsförstärkare – felaktig funktion",
    625: "Hastighetssignal – felaktig",
    668: "Matningsspänning klämma 30 – utanför tolerans",
    281: "Hastighetsgivare G22 – felaktig signal",
    283: "ABS-hjulgivare – felaktig signal",
    778: "Rattvinkelgivare G85 – felaktig signal",
    779: "Yttertemperaturgivare G17 – felaktig signal",
    # -- Ställdon (VP37/EDC15) ----------------------------------------------
    1247: "Kallstartventil – elfel i kretsen",
    1248: "Insprutningsstartventil N108 – elfel i kretsen",
    1249: "Insprutningsventil cylinder 1 N30 – elfel i kretsen",
    1250: "Insprutningsventil cylinder 2 N31 – elfel i kretsen",
    1251: "Insprutningsventil cylinder 3 N32 – elfel i kretsen",
    1252: "Insprutningsventil cylinder 4 N33 – elfel i kretsen",
    1253: "Avgasåterföringsventil N18 – elfel i kretsen",
    1257: "Tomgångsstabilisering – felaktig funktion",
    1259: "Bränslepumprelä J17 – elfel i kretsen",
    1262: "Laddtrycksventil N75 – elfel i kretsen",
    1265: "Avgasåterföringsventil N18 – elfel i kretsen",
    1266: "Glödstiftsrelä J52 – elfel i kretsen",
    1268: "Mängdställare N146 – elfel i kretsen",
    1269: "Insprutningsstoppventil N109 – elfel i kretsen",
    1271: "Kylarfläkt – elfel i kretsen",
    1274: "Motorstyrdonets relä – elfel i kretsen",
    # -- P-kodsområdet (16384+) ---------------------------------------------
    16485: "Luftmassemätare G70 – signal utanför tolerans (P0101)",
    16486: "Luftmassemätare G70 – signal för låg (P0102)",
    16487: "Luftmassemätare G70 – signal för hög (P0103)",
    16496: "Insugslufttemperaturgivare G42 – signal för låg",
    16497: "Insugslufttemperaturgivare G42 – signal för hög",
    16500: "Kylvätsketemperaturgivare G62 – signal utanför tolerans",
    16501: "Kylvätsketemperaturgivare G62 – signal för låg",
    16502: "Kylvätsketemperaturgivare G62 – signal för hög",
    16505: "Bränsletemperaturgivare G81 – signal utanför tolerans",
    16506: "Bränsletemperaturgivare G81 – signal för låg",
    16507: "Bränsletemperaturgivare G81 – signal för hög",
    16514: "Lambdasond G39 – krets felaktig",
    16554: "Cylinder 1 – insprutningskrets felaktig",
    16555: "Cylinder 2 – insprutningskrets felaktig",
    16556: "Cylinder 3 – insprutningskrets felaktig",
    16557: "Cylinder 4 – insprutningskrets felaktig",
    16684: "Slumpmässig förbränningsmiss upptäckt (P0300)",
    16685: "Cylinder 1 – förbränningsmiss",
    16686: "Cylinder 2 – förbränningsmiss",
    16687: "Cylinder 3 – förbränningsmiss",
    16688: "Cylinder 4 – förbränningsmiss",
    16705: "Motorvarvtalsgivare G28 – felaktig signal",
    16706: "Motorvarvtalsgivare G28 – ingen signal",
    16785: "Avgasåterföring EGR – otillräckligt flöde (P0401)",
    16786: "Avgasåterföring EGR – för högt flöde (P0402)",
    16787: "Avgasåterföringsventil N18 – elfel i kretsen (P0403)",
    16792: "Avgasåterföringens temperaturgivare – utanför tolerans",
    16825: "Bränsletankventil N80 – elfel i kretsen",
    16989: "Tomgångsreglering – varvtal utanför tolerans",
    17091: "Insugslufttemperatur – för hög (effektreducering)",
    17509: "Lambdasond – uppvärmningskrets felaktig",
    17544: "Bränsleblandning för fet (bank 1)",
    17545: "Bränsleblandning för mager (bank 1)",
    17604: "Insprutningsstartventil N108 – kortslutning till plus",
    17605: "Insprutningsstartventil N108 – kortslutning till jord",
    17606: "Insprutningsstartventil N108 – avbrott",
    17610: "Mängdställare N146 – reglergräns nådd",
    17638: "Motorstyrdon – självdiagnos otillräcklig",
    17800: "Bromsljuskontakt F – implausibel signal",
    17811: "Avgasåterföring EGR – regleravvikelse (P1403)",
    17815: "Laddtrycksgivare G31 – implausibel signal",
    17816: "Laddtrycksgivare G31 – signal för låg",
    17817: "Laddtrycksgivare G31 – signal för hög",
    17910: "Insprutningsstart – regleravvikelse",
    17911: "Insprutningsstart – reglergräns underskriden",
    17912: "Insprutningsstart – reglergräns överskriden",
    17963: "Laddtrycksreglering – reglergräns ej nådd",
    17964: "Laddtrycksreglering – negativ avvikelse (P1556)",
    17965: "Laddtrycksreglering – positiv avvikelse (P1557)",
    17966: "Gasspjällställdon – elfel i kretsen (P1558)",
    17967: "Gasspjälläge – adaption felaktig",
    17978: "Immobilizer – ingen auktorisering / spärrad",
    17979: "Immobilizer – ingen signal från styrdon",
    18010: "Matningsspänning klämma 30 – för låg (P1602)",
    18011: "Matningsspänning – för hög",
    18013: "Höjdgivare – implausibel signal",
    18034: "Motorstyrdon – matningsspänning för låg",
    18265: "Lastsignal från motorstyrdon – felaktig",
    19289: "Glödstiftsrelä J52 – elfel i kretsen",
    # -- Kombiinstrument / övrigt -------------------------------------------
    1039: "Belysning kombiinstrument – elfel i kretsen",
    1041: "Ljudsignal – elfel i kretsen",
    1122: "Bränslemätargivare G – felaktig signal",
    2020: "Varningslampa (MIL) – elfel i kretsen / lampa trasig",
}


def beskrivning(kod: int) -> tuple[str, bool]:
    """Returnera (klartext, känd). Okänd kod ger en googlingsuppmaning."""
    text = FELKODER.get(kod)
    if text:
        return text, True
    return (
        f"Okänd felkod – sök på nätet efter \"VAG felkod {kod:05d}\"",
        False,
    )


@dataclass(frozen=True)
class Felkod:
    """En avkodad felkod ur styrdonets minne."""

    kod: int
    status: int
    text: str
    kand: bool = True

    @property
    def nummer(self) -> str:
        """Femsiffrigt VAG-nummer som text, t.ex. ``17965``."""
        return f"{self.kod:05d}"

    @property
    def sporadisk(self) -> bool:
        """True om felet är sporadiskt (bit 0x80 i statusbyten)."""
        return bool(self.status & BIT_SPORADISK)

    @property
    def statustext(self) -> str:
        """Statusbyten i klartext."""
        return statustext(self.status)

    def __str__(self) -> str:
        markor = " [SP]" if self.sporadisk else ""
        return f"{self.nummer}{markor} – {self.text}\n        ({self.statustext})"


def avkoda(hog: int, lag: int, status: int) -> Felkod:
    """Avkoda en felkod ur tre råa bytes."""
    kod = ((hog & 0xFF) << 8) | (lag & 0xFF)
    text, kand = beskrivning(kod)
    return Felkod(kod, status & 0xFF, text, kand)


def avkoda_block(data: bytes) -> list[Felkod]:
    """Avkoda ett helt 0xFC-block. Tomma platser (0xFFFF) filtreras bort."""
    koder: list[Felkod] = []
    for i in range(0, len(data) - 2, 3):
        hog, lag, status = data[i], data[i + 1], data[i + 2]
        if ((hog << 8) | lag) == INGEN_FELKOD:
            continue
        koder.append(avkoda(hog, lag, status))
    return koder


# ---------------------------------------------------------------------------
# Ställdon (komponentkoder i 0xF5-svar)
# ---------------------------------------------------------------------------

#: Komponentkod -> namn. Ungefärlig tabell; råvärdet visas alltid.
STALLDON: dict[int, str] = {
    0x0000: "Ställdonstest avslutat",
    0x0101: "Insprutningsstartventil N108",
    0x0102: "Avgasåterföringsventil N18 (EGR)",
    0x0103: "Laddtrycksventil N75",
    0x0104: "Insugsrörsspjäll / gasspjällställdon",
    0x0105: "Glödstiftsrelä J52",
    0x0106: "Bränslepumprelä J17",
    0x0107: "Mängdställare N146",
    0x0108: "Insprutningsstoppventil N109",
    0x0109: "Kylarfläkt låg hastighet",
    0x010A: "Kylarfläkt hög hastighet",
    0x010B: "AC-kompressorkoppling",
    0x010C: "Varningslampa (MIL) K83",
    0x010D: "Glödlampa förvärmning K29",
    0x010E: "Tankavluftningsventil N80",
}


def stalldonsnamn(kod: int) -> str:
    """Namn på ställdon ur komponentkoden. Okänd kod visas som hex."""
    namn = STALLDON.get(kod)
    if namn:
        return namn
    return f"Okänt ställdon (komponentkod 0x{kod:04X} / {kod})"
