"""Undantagshierarki för VAGDIAG.

Alla undantag bär ett svenskt meddelande och (valfritt) en ``tips``-text som
menyn kan visa för användaren i stället för en rå traceback.
"""

from __future__ import annotations

__all__ = [
    "VagdiagFel",
    "TransportFel",
    "KWPFel",
    "KWPTimeout",
    "KWPProtokollFel",
    "KWPAnslutningsFel",
    "KWPNekat",
]

_STANDARDTIPS = (
    "Kontrollera att tändningen är PÅ, att KKL-kabeln sitter i OBD-uttaget och "
    "att FTDI Latency Timer är satt till 1 ms. Slå av tändningen i 5 sekunder "
    "och försök igen."
)


class VagdiagFel(Exception):
    """Basklass för alla fel i VAGDIAG."""

    def __init__(self, meddelande: str, tips: str | None = None) -> None:
        super().__init__(meddelande)
        self.meddelande = meddelande
        self.tips = tips

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.meddelande


class TransportFel(VagdiagFel):
    """Fel på det fysiska lagret (serieport kunde inte öppnas, m.m.)."""

    def __init__(self, meddelande: str, tips: str | None = None) -> None:
        super().__init__(
            meddelande,
            tips or "Kontrollera COM-porten med --lista-portar och att ingen "
            "annan programvara (t.ex. VCDS) håller porten öppen.",
        )


class KWPFel(VagdiagFel):
    """Basklass för protokollfel i KWP1281."""

    def __init__(self, meddelande: str, tips: str | None = None) -> None:
        super().__init__(meddelande, tips or _STANDARDTIPS)


class KWPTimeout(KWPFel):
    """Styrdonet svarade inte inom utsatt tid. Sessionen är död."""


class KWPProtokollFel(KWPFel):
    """Felaktigt kvitteringsbyte, fel blockformat eller oväntad blocktitel."""


class KWPAnslutningsFel(KWPFel):
    """5-baud-initieringen misslyckades – inget styrdon svarade."""


class KWPNekat(KWPFel):
    """Styrdonet nekade kommandot (svarade med annan titel än förväntat)."""

    def __init__(self, meddelande: str, tips: str | None = None) -> None:
        super().__init__(
            meddelande,
            tips or "Funktionen stöds troligen inte av det här styrdonet, "
            "eller kräver login först.",
        )
