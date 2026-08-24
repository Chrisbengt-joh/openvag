"""Mätvärdesformler för KWP1281.

Ett mätvärde levereras av styrdonet som tre bytes: ``(formel_id, a, b)``.
Formel-id:t väljer omräkningsformel och enhet. Tabellen nedan är datadriven –
lägg till eller ersätt formler med :func:`registrera`.

**Om tillförlitligheten:** id 1–21 samt 25 är väl dokumenterade och stämmer med
VCDS i praktiken. Id 22 och uppåt är rekonstruerade från publika källor och
loggjämförelser; de är markerade ``verifierad=False`` och visas med ett
efterföljande ``?`` i gränssnittet. Jämför alltid mot VCDS innan du drar
slutsatser av ett värde med ``?``. Okänt id kraschar aldrig – det visas som
``raw(id,a,b)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union

__all__ = [
    "Formel",
    "Matvarde",
    "FORMLER",
    "berakna",
    "registrera",
    "koda",
    "formatera",
]

#: Ett beräknat mätvärde är antingen ett tal, en text (t.ex. bitfält) eller None.
Varde = Union[float, str, None]


@dataclass(frozen=True)
class Formel:
    """En omräkningsformel för ett mätvärde."""

    id: int
    enhet: str
    beskrivning: str
    funktion: Callable[[int, int], Varde]
    verifierad: bool = True


@dataclass(frozen=True)
class Matvarde:
    """Ett färdigt omräknat mätvärde."""

    formel_id: int
    a: int
    b: int
    varde: Varde
    enhet: str
    verifierad: bool = True
    kand: bool = True

    @property
    def ar_tal(self) -> bool:
        """True om värdet är numeriskt (går att logga/plotta)."""
        return isinstance(self.varde, (int, float)) and not isinstance(self.varde, bool)

    @property
    def tal(self) -> float | None:
        """Värdet som float, eller None om det inte är numeriskt."""
        return float(self.varde) if self.ar_tal else None  # type: ignore[arg-type]

    @property
    def text(self) -> str:
        """Formaterat värde med enhet, t.ex. ``2450 1/min``."""
        if not self.kand or self.varde is None:
            return f"raw({self.formel_id},{self.a},{self.b})"
        if isinstance(self.varde, str):
            return self.varde
        flagga = "" if self.verifierad else " ?"
        enhet = f" {self.enhet}" if self.enhet else ""
        return f"{formatera(float(self.varde))}{enhet}{flagga}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def formatera(v: float) -> str:
    """Formatera ett tal med lagom antal decimaler för terminalvisning."""
    a = abs(v)
    if a >= 1000:
        return f"{v:.0f}"
    if a >= 100:
        return f"{v:.1f}"
    if a >= 10:
        return f"{v:.2f}"
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def _bitfalt(a: int, b: int) -> str:
    """Id 16: åtta binära flaggor i ``b``. ``a`` är en mask över aktiva bitar."""
    bitar = f"{b:08b}"
    if a in (0, 0xFF):
        return bitar
    return f"{bitar} (mask {a:08b})"


def _ascii(a: int, b: int) -> str:
    """Id 17: två ASCII-tecken."""
    tecken = "".join(chr(x) if 32 <= x < 127 else "." for x in (a, b))
    return tecken


def _kall_varm(a: int, b: int) -> str:
    """Id 10: binär temperaturflagga."""
    return "varm" if b else "kall"


def _tid_mm_ss(a: int, b: int) -> str:
    """Id 44: minuter:sekunder."""
    return f"{a:d}:{b:02d}"


def _sakert_delat(taljare: float, namnare: float) -> float:
    """Division som ger 0 i stället för att kasta vid nämnare 0."""
    return taljare / namnare if namnare else 0.0


# ---------------------------------------------------------------------------
# Formeltabellen
# ---------------------------------------------------------------------------

_TABELL: list[Formel] = [
    Formel(0, "", "ej använd", lambda a, b: None),
    Formel(1, "1/min", "varvtal", lambda a, b: 0.2 * a * b),
    Formel(2, "%", "andel", lambda a, b: a * 0.002 * b),
    Formel(3, "grader", "vinkel", lambda a, b: 0.002 * a * b),
    Formel(4, "°FÖD", "vinkel före övre dödpunkt", lambda a, b: abs(b - 127) * 0.01 * a),
    Formel(5, "°C", "temperatur", lambda a, b: a * (b - 100) * 0.1),
    Formel(6, "V", "spänning", lambda a, b: 0.001 * a * b),
    Formel(7, "km/h", "hastighet", lambda a, b: 0.01 * a * b),
    Formel(8, "", "obenämnt tal", lambda a, b: 0.1 * a * b),
    Formel(9, "grader", "vinkel (signerad)", lambda a, b: (b - 127) * 0.02 * a),
    Formel(10, "", "kall/varm", _kall_varm),
    Formel(11, "", "faktor", lambda a, b: 0.0001 * a * (b - 128) + 1),
    Formel(12, "ohm", "resistans", lambda a, b: 0.001 * a * b),
    Formel(13, "mm", "längd (signerad)", lambda a, b: (b - 127) * 0.001 * a),
    Formel(14, "bar", "tryck", lambda a, b: 0.005 * a * b),
    Formel(15, "ms", "tid", lambda a, b: 0.01 * a * b),
    Formel(16, "", "bitfält", _bitfalt),
    Formel(17, "", "ASCII-par", _ascii),
    Formel(18, "mbar", "tryck", lambda a, b: 0.04 * a * b),
    Formel(19, "l", "volym", lambda a, b: a * b * 0.01),
    Formel(20, "%", "andel (signerad)", lambda a, b: a * (b - 128) / 128.0),
    Formel(21, "V", "spänning", lambda a, b: 0.001 * a * b),
    # -- härifrån och nedåt: rekonstruerat, jämför mot VCDS --------------------
    Formel(22, "", "kvot", lambda a, b: _sakert_delat(a * 2000.0, b), verifierad=False),
    Formel(23, "%", "andel", lambda a, b: b / 256.0 * a, verifierad=False),
    Formel(24, "A", "ström", lambda a, b: a * b / 32768.0, verifierad=False),
    Formel(25, "g/s", "luftmassa", lambda a, b: (b * 1.421) + (a / 182.0)),
    Formel(26, "°C", "temperaturdifferens", lambda a, b: float(b - a), verifierad=False),
    Formel(27, "mg/slag", "insprutad mängd (signerad)",
           lambda a, b: abs(b - 128) * 0.01 * a, verifierad=False),
    Formel(28, "", "differens", lambda a, b: float(b - a), verifierad=False),
    Formel(29, "", "absolut differens", lambda a, b: float(abs(b - a)), verifierad=False),
    Formel(30, "grader k/v", "vinkel per vev", lambda a, b: b / 12.0 * a, verifierad=False),
    Formel(31, "°C", "temperatur", lambda a, b: b / 2560.0 * a, verifierad=False),
    Formel(32, "", "signerat tal", lambda a, b: float(b - 256 if b > 128 else b),
           verifierad=False),
    Formel(33, "%", "andel", lambda a, b: _sakert_delat(100.0 * a, b), verifierad=False),
    Formel(34, "kW", "effekt", lambda a, b: (b - 128) * 0.01 * a, verifierad=False),
    Formel(35, "l/h", "flöde", lambda a, b: 0.01 * a * b, verifierad=False),
    Formel(36, "km", "sträcka", lambda a, b: float(a * 2560 + b * 10), verifierad=False),
    Formel(37, "s", "tid", lambda a, b: float(a * 256 + b) * 0.01, verifierad=False),
    Formel(38, "grader k/v", "vinkel (signerad)", lambda a, b: (b - 128) * 0.001 * a,
           verifierad=False),
    Formel(39, "mg/slag", "mängdavvikelse (signerad)", lambda a, b: (b - 128) * 0.1 * a,
           verifierad=False),
    Formel(40, "A", "ström", lambda a, b: b * 0.1 + (25.5 * a) - 400.0, verifierad=False),
    Formel(41, "Ah", "laddning", lambda a, b: float(b + a * 255), verifierad=False),
    Formel(42, "kW", "effekt", lambda a, b: b * 0.1 + (25.5 * a) - 400.0, verifierad=False),
    Formel(43, "V", "spänning", lambda a, b: b * 0.1 + (25.5 * a), verifierad=False),
    Formel(44, "", "tid mm:ss", _tid_mm_ss, verifierad=False),
    Formel(45, "", "andel", lambda a, b: 0.1 * a * b / 100.0, verifierad=False),
    Formel(46, "mbar", "tryckdifferens", lambda a, b: (a * b - 3200) * 0.0027,
           verifierad=False),
    Formel(47, "ms", "tid (signerad)", lambda a, b: float((b - 128) * a), verifierad=False),
    Formel(48, "", "råvärde 16-bit", lambda a, b: float(b + a * 255), verifierad=False),
    Formel(49, "mg/slag", "insprutad mängd", lambda a, b: (b / 4.0) * a * 0.1,
           verifierad=False),
    Formel(50, "mbar", "tryck (signerad)", lambda a, b: _sakert_delat(a * (b - 128), 128.0),
           verifierad=False),
    Formel(51, "mg/slag", "luftmassa/mängd", lambda a, b: 0.1 * a * b, verifierad=False),
    Formel(52, "Nm", "moment", lambda a, b: b * 0.02 * a - a, verifierad=False),
    Formel(53, "g/s", "luftmassa (signerad)",
           lambda a, b: (b - 128) * 1.4222 + 0.006 * a, verifierad=False),
    Formel(54, "", "räknare", lambda a, b: float(a * 256 + b), verifierad=False),
]

#: Uppslagstabell formel-id -> :class:`Formel`.
FORMLER: dict[int, Formel] = {f.id: f for f in _TABELL}


def registrera(formel: Formel) -> None:
    """Lägg till eller ersätt en formel i tabellen (t.ex. efter VCDS-jämförelse)."""
    FORMLER[formel.id] = formel


def berakna(formel_id: int, a: int, b: int) -> Matvarde:
    """Räkna om ``(formel_id, a, b)`` till ett :class:`Matvarde`.

    Kastar aldrig – okänt id eller trasig formel ger ett råvärde i stället.
    """
    formel = FORMLER.get(formel_id)
    if formel is None:
        return Matvarde(formel_id, a, b, None, "", verifierad=False, kand=False)
    try:
        varde = formel.funktion(a, b)
    except Exception:
        return Matvarde(formel_id, a, b, None, formel.enhet, verifierad=False, kand=False)
    return Matvarde(formel_id, a, b, varde, formel.enhet, formel.verifierad)


def koda(formel_id: int, onskat: float, a: int = 100) -> tuple[int, int, int]:
    """Invers av :func:`berakna` – hitta ``(formel_id, a, b)`` för ett önskat värde.

    Används av ECU-simulatorn för att servera realistiska mätvärden. ``b`` söks
    igenom uttömmande (0–255) för det givna ``a`` och den bästa träffen väljs.
    """
    formel = FORMLER.get(formel_id)
    a &= 0xFF
    if formel is None:
        return (formel_id & 0xFF, a, 0)
    bast_b = 0
    bast_fel = float("inf")
    for b in range(256):
        try:
            v = formel.funktion(a, b)
        except Exception:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        fel = abs(float(v) - onskat)
        if fel < bast_fel:
            bast_fel, bast_b = fel, b
            if fel == 0.0:
                break
    return (formel_id & 0xFF, a, bast_b)
