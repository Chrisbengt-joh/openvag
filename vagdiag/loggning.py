"""CSV-loggning av mätvärden under provkörning.

Varje rad flushas till disk direkt, så en logg överlever att programmet
kraschar eller att kabeln rycks ur mitt i en mätning. Rubrikraden skrivs
när det första mätvärdet kommer in, eftersom enheterna först då är kända.

Standardformatet är semikolon som avgränsare och decimalkomma, vilket svensk
Excel öppnar direkt. Vill du ha punkt och komma (för pandas, gnuplot m.m.)
skickar du ``avgransare=","`` och ``decimalkomma=False``.
"""

from __future__ import annotations

import csv
import datetime as _dt
import time
from pathlib import Path
from typing import IO, Any, Mapping, Sequence

from .formler import Matvarde
from .styrdon import etikett

__all__ = ["CsvLogg"]


class CsvLogg:
    """Skriver mätvärden till en CSV-fil, en rad per avläsningsvarv."""

    def __init__(
        self,
        filnamn: str | Path,
        grupper: Sequence[int],
        adress: int = 0x01,
        avgransare: str = ";",
        decimalkomma: bool = True,
        varden_per_grupp: int = 4,
    ) -> None:
        self.filnamn = Path(filnamn)
        self.grupper = list(grupper)
        self.adress = adress
        self.avgransare = avgransare
        self.decimalkomma = decimalkomma
        self.varden_per_grupp = varden_per_grupp
        self.rader = 0
        self._start = time.monotonic()
        self._rubrik_skriven = False
        self._fil: IO[str] | None = None
        self._skrivare: Any | None = None  # csv.writer har ingen publik typ

        self.filnamn.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig gör att svensk Excel visar å/ä/ö rätt.
        self._fil = open(self.filnamn, "w", newline="", encoding="utf-8-sig")
        self._skrivare = csv.writer(self._fil, delimiter=self.avgransare)

    # -- rubrik ------------------------------------------------------------

    def _kolumnnamn(self, matningar: Mapping[int, Sequence[Matvarde]]) -> list[str]:
        """Bygg rubrikrad: ``003.2 Luftmassa BÖR [mg/slag]``."""
        namn = ["tidpunkt", "sekunder"]
        for grupp in self.grupper:
            varden = matningar.get(grupp, ())
            for pos in range(1, self.varden_per_grupp + 1):
                text = etikett(grupp, pos, self.adress)
                enhet = ""
                if pos - 1 < len(varden):
                    enhet = varden[pos - 1].enhet
                enhetstext = f" [{enhet}]" if enhet else ""
                namn.append(f"{grupp:03d}.{pos} {text}{enhetstext}")
        return namn

    # -- skrivning ---------------------------------------------------------

    def _formatera(self, matvarde: Matvarde | None) -> str:
        """Ett cellvärde: tal om möjligt, annars texten, annars tomt."""
        if matvarde is None:
            return ""
        if matvarde.ar_tal:
            text = f"{matvarde.tal:.3f}"
            return text.replace(".", ",") if self.decimalkomma else text
        if isinstance(matvarde.varde, str):
            return matvarde.varde
        return matvarde.text

    def logga(self, matningar: Mapping[int, Sequence[Matvarde]]) -> None:
        """Skriv en rad med alla värden från ett avläsningsvarv."""
        if self._skrivare is None or self._fil is None:
            raise ValueError("Loggen är stängd.")
        if not self._rubrik_skriven:
            self._skrivare.writerow(self._kolumnnamn(matningar))
            self._rubrik_skriven = True

        nu = _dt.datetime.now()
        rad: list[str] = [
            nu.strftime("%Y-%m-%d %H:%M:%S.") + f"{nu.microsecond // 1000:03d}",
            self._formatera_sekunder(time.monotonic() - self._start),
        ]
        for grupp in self.grupper:
            varden = list(matningar.get(grupp, ()))
            for pos in range(self.varden_per_grupp):
                rad.append(self._formatera(varden[pos] if pos < len(varden) else None))
        self._skrivare.writerow(rad)
        self._fil.flush()  # kraschsäkert: raden finns på disk direkt
        self.rader += 1

    def _formatera_sekunder(self, s: float) -> str:
        text = f"{s:.3f}"
        return text.replace(".", ",") if self.decimalkomma else text

    # -- livscykel ---------------------------------------------------------

    def stang(self) -> None:
        """Stäng filen."""
        if self._fil is not None:
            self._fil.close()
            self._fil = None
            self._skrivare = None

    def __enter__(self) -> CsvLogg:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stang()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.filnamn} ({self.rader} rader)"
