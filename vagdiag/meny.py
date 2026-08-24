"""Terminalgränssnitt för VAGDIAG.

All ECU-kommunikation går via :class:`vagdiag.kwp1281.KWP1281`; det här
lagret sköter bara utskrifter, inmatning och bekräftelser. Varje kommando
fångar :class:`vagdiag.undantag.VagdiagFel` och visar ett svenskt
felmeddelande med tips – användaren ska aldrig se en traceback.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Callable, Iterable, Sequence

from .felkoder import Felkod
from .formler import Matvarde
from .kwp1281 import KWP1281, Identifikation, Skanresultat, skanna
from .loggning import CsvLogg
from .styrdon import (
    ADRESSER,
    AUTOSCAN_ADRESSER,
    KANSLIGA_ADRESSER,
    etikett,
    grupp_ar_kand,
    gruppnamn,
    styrdonsnamn,
)
from .transport import Transport
from .undantag import VagdiagFel

__all__ = ["Meny", "forbered_terminal", "aktivera_ansi", "Tangentbord"]

BREDD = 78

#: Sant när terminalen bevisligen klarar UTF-8 (sätts av forbered_terminal).
#: Windows-konsolen kör annars cp1252, som saknar ramtecken – då används ASCII.
_UNICODE = False


# ---------------------------------------------------------------------------
# Terminalhjälpmedel
# ---------------------------------------------------------------------------


def aktivera_ansi() -> bool:
    """Slå på ANSI-koder i Windows-konsolen. True om det gick."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handtag = kernel32.GetStdHandle(-11)
        lage = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handtag, ctypes.byref(lage)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handtag, lage.value | 0x0004))
    except Exception:
        return False


def _satt_utf8() -> bool:
    """Ställ om konsol och strömmar till UTF-8. True om det gick fullt ut."""
    ok = True
    if os.name == "nt":
        try:
            import ctypes

            if not ctypes.windll.kernel32.SetConsoleOutputCP(65001):  # type: ignore[attr-defined]
                ok = False
        except Exception:
            ok = False
    for strom in (sys.stdout, sys.stderr):
        try:
            # errors="replace" gör att en udda teckenuppsättning aldrig
            # kraschar programmet mitt i en mätning.
            strom.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            ok = False
    return ok


def forbered_terminal() -> bool:
    """Slå på ANSI och UTF-8. Anropas en gång vid start."""
    global _UNICODE
    ansi = aktivera_ansi()
    _UNICODE = _satt_utf8()
    return ansi and _UNICODE


def _ram() -> dict[str, str]:
    """Ramtecken – snygga när terminalen klarar UTF-8, annars rena ASCII."""
    if _UNICODE:
        return {"h": "═", "v": "║", "nv": "╔", "nh": "╗", "sv": "╚",
                "sh": "╝", "vt": "╠", "ht": "╣", "tunn": "─"}
    return {"h": "=", "v": "|", "nv": "+", "nh": "+", "sv": "+",
            "sh": "+", "vt": "+", "ht": "+", "tunn": "-"}


class Tangentbord:
    """Läser enstaka tangenttryck utan att blockera.

    Används i liveläget så att q/l fungerar utan att man behöver trycka Enter.
    Om stdin inte är en terminal (t.ex. omdirigerad) returneras alltid None.
    """

    def __init__(self) -> None:
        self._aktiv = False
        self._gammalt: object | None = None
        self._fd: int | None = None

    def __enter__(self) -> Tangentbord:
        if os.name == "nt":
            self._aktiv = True
            return self
        try:
            import termios
            import tty

            if not sys.stdin.isatty():
                return self
            self._fd = sys.stdin.fileno()
            self._gammalt = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._aktiv = True
        except Exception:
            self._aktiv = False
        return self

    def __exit__(self, *_exc: object) -> None:
        if os.name != "nt" and self._fd is not None and self._gammalt is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._gammalt)
            except Exception:
                pass
        self._aktiv = False

    def las(self) -> str | None:
        """Returnera nedtryckt tangent, eller None om ingen tryckts."""
        if not self._aktiv:
            return None
        if os.name == "nt":
            try:
                import msvcrt

                if not msvcrt.kbhit():
                    return None
                return msvcrt.getwch()
            except Exception:
                # Ingen riktig konsol (omdirigerad stdin) – ingen tangent.
                self._aktiv = False
                return None
        import select

        klar, _, _ = select.select([sys.stdin], [], [], 0)
        if not klar:
            return None
        return sys.stdin.read(1)


def rensa_skarm() -> None:
    """Rensa skärmen och ställ markören överst."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def rita(rader: Sequence[str]) -> None:
    """Rita om skärmen utan flimmer.

    Markören flyttas hem och varje rad skrivs över befintligt innehåll
    (``\\x1b[K`` rensar resten av raden). Ingen full clear = inget flimmer.
    """
    ut = ["\x1b[H"]
    for rad in rader:
        ut.append(rad + "\x1b[K\n")
    ut.append("\x1b[0J")
    sys.stdout.write("".join(ut))
    sys.stdout.flush()


def rubrik(text: str) -> str:
    """En understruken rubrikrad."""
    return f"\n{text}\n{_ram()['tunn'] * min(BREDD, len(text) + 2)}"


def linje(tecken: str = "tunn") -> str:
    """En vågrät avdelare över hela bredden."""
    return _ram()[tecken] * BREDD


def varning(rader: Iterable[str]) -> None:
    """Skriv en tydlig varningsruta."""
    r = _ram()
    print()
    print(r["nv"] + r["h"] * (BREDD - 2) + r["nh"])
    print(r["v"] + " VARNING ".center(BREDD - 2, " ") + r["v"])
    print(r["vt"] + r["h"] * (BREDD - 2) + r["ht"])
    for rad in rader:
        for bit in _bryt(rad, BREDD - 4):
            print(r["v"] + " " + bit.ljust(BREDD - 4) + " " + r["v"])
    print(r["sv"] + r["h"] * (BREDD - 2) + r["sh"])


def _bryt(text: str, bredd: int) -> list[str]:
    """Enkel radbrytning på ordgräns."""
    if not text:
        return [""]
    rader: list[str] = []
    rad = ""
    for ord_ in text.split(" "):
        if len(rad) + len(ord_) + 1 > bredd and rad:
            rader.append(rad)
            rad = ord_
        else:
            rad = f"{rad} {ord_}".strip()
    rader.append(rad)
    return rader


def visa_fel(fel: VagdiagFel) -> None:
    """Visa ett fel begripligt, aldrig som traceback."""
    print(f"\n  FEL: {fel.meddelande}")
    if fel.tips:
        for rad in _bryt(f"Tips: {fel.tips}", BREDD - 8):
            print(f"       {rad}")
    print()


# ---------------------------------------------------------------------------
# Inmatning
# ---------------------------------------------------------------------------


def fraga(text: str, standard: str = "") -> str:
    """Fråga efter en textrad. Tom rad ger standardvärdet."""
    ledtext = f"{text} [{standard}]: " if standard else f"{text}: "
    try:
        svar = input(ledtext).strip()
    except EOFError:
        return standard
    return svar or standard


def fraga_heltal(text: str, standard: int | None = None,
                 lagsta: int = 0, hogsta: int = 65535) -> int | None:
    """Fråga efter ett heltal inom ett intervall. None vid avbrott."""
    while True:
        svar = fraga(text, "" if standard is None else str(standard))
        if not svar:
            return standard
        try:
            varde = int(svar, 0)
        except ValueError:
            print(f"  '{svar}' är inte ett tal.")
            continue
        if not lagsta <= varde <= hogsta:
            print(f"  Ange ett tal mellan {lagsta} och {hogsta}.")
            continue
        return varde


def fraga_exakt(text: str, kravord: str) -> bool:
    """Kräv att användaren skriver exakt ``kravord`` (versalkänsligt)."""
    svar = fraga(f"{text} (skriv {kravord} för att fortsätta)")
    if svar == kravord:
        return True
    print("  Avbrutet – inget gjordes.")
    return False


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def formatera_matvarden(
    grupp: int, varden: Sequence[Matvarde], adress: int = 0x01
) -> list[str]:
    """Rader för ett mätvärdesblock."""
    huvud = f"Grupp {grupp:03d}  {gruppnamn(grupp, adress)}"
    if not grupp_ar_kand(grupp, adress):
        huvud += "  (etiketter saknas)"
    rader = [huvud]
    if not varden:
        rader.append("    (styrdonet har ingen sådan grupp)")
        return rader
    for i, varde in enumerate(varden, start=1):
        namn = etikett(grupp, i, adress)[:32]
        rader.append(f"    {i}  {namn:<32} {varde.text:>22}")
    return rader


def formatera_felkoder(koder: Sequence[Felkod]) -> list[str]:
    """Rader för en felkodslista."""
    if not koder:
        return ["  Inga felkoder lagrade."]
    rader = [f"  {len(koder)} felkod(er) hittade:", ""]
    for kod in koder:
        markor = "SP" if kod.sporadisk else "  "
        rader.append(f"  {kod.nummer} [{markor}] {kod.text}")
        rader.append(f"              {kod.statustext}")
        rader.append("")
    return rader


def formatera_ident(ident: Identifikation) -> list[str]:
    """Rader för identifikationsuppgifter."""
    rader = [
        f"  Styrdon      0x{ident.adress:02X}  {ident.namn}",
        f"  Delnummer    {ident.delnummer or '(okänt)'}",
        f"  Komponent    {ident.komponent or '(okänd)'}",
    ]
    if ident.kodning is not None:
        rader.append(f"  Kodning      {ident.kodning}")
    if ident.wsc is not None:
        rader.append(f"  Verkstad     WSC {ident.wsc}")
    rader.append(f"  Nyckelbytes  0x{ident.kb1:02X} 0x{ident.kb2:02X}")
    if ident.textrader:
        rader.append("  Rådata       " + " | ".join(ident.textrader))
    return rader


# ---------------------------------------------------------------------------
# Menyn
# ---------------------------------------------------------------------------


class Meny:
    """Menystyrt terminalgränssnitt mot ett KWP1281-styrdon."""

    def __init__(
        self,
        transport: Transport,
        spar: Callable[[str], None] | None = None,
        avgransare: str = ";",
        decimalkomma: bool = True,
        intervall: float = 0.0,
        timeout: float = 1.0,
        init_timeout: float = 2.0,
    ) -> None:
        self.transport = transport
        self.spar = spar
        self.avgransare = avgransare
        self.decimalkomma = decimalkomma
        self.intervall = intervall
        self.timeout = timeout
        self.init_timeout = init_timeout
        self.klient: KWP1281 | None = None
        forbered_terminal()

    # -- livscykel ---------------------------------------------------------

    def kor(self) -> int:
        """Kör huvudloopen tills användaren avslutar. Returnerar exitkod."""
        self._valkommen()
        try:
            while True:
                try:
                    if not self._huvudmeny():
                        return 0
                except VagdiagFel as fel:
                    visa_fel(fel)
                    self.koppla_ner()
        except KeyboardInterrupt:
            print("\n\nAvbrutet.")
            return 130
        finally:
            self.koppla_ner()

    def _valkommen(self) -> None:
        print()
        print(linje("h"))
        print("  VAGDIAG – diagnos för äldre VAG-bilar (KWP1281 över K-line)")
        print(f"  Anslutning: {self.transport.namn}")
        print("  Hobbyverktyg – används på egen risk. VCDS är facit.")
        print(linje("h"))
        latens = getattr(self.transport, "latens_ms", None)
        if latens is not None and latens > 2:
            varning([
                f"FTDI Latency Timer är {latens} ms. KWP1281 är tidskänsligt och "
                "kommunikationen blir opålitlig över 2 ms.",
                "",
                "Sätt den till 1 ms: Enhetshanteraren -> Portar (COM & LPT) -> "
                "din USB Serial Port -> Egenskaper -> Portinställningar -> "
                "Avancerat -> Latency Timer (msec) = 1.",
            ])

    def koppla_ner(self) -> None:
        if self.klient is not None:
            try:
                self.klient.koppla_ner()
            except VagdiagFel:
                pass
            self.klient = None

    # -- huvudmeny ---------------------------------------------------------

    def _huvudmeny(self) -> bool:
        """Visa menyn och utför valet. Returnerar False när programmet ska avslutas."""
        self._kontrollera_bakgrundsfel()
        print(rubrik("HUVUDMENY"))
        if self.klient and self.klient.ansluten and self.klient.ident:
            print(f"  Ansluten: {self.klient.ident.sammanfattning()}")
            print()
            print("   1  Läs felkoder")
            print("   2  Radera felkoder")
            print("   3  Mätgrupper live (med valfri CSV-logg)")
            print("   4  Grundinställning")
            print("   5  Ställdonstest")
            print("   6  Anpassning")
            print("   7  Login")
            print("   8  Visa identifikation")
            print("   9  Koppla ner")
            print("   0  Avsluta")
            val = fraga("\n  Val")
            return self._utfor_ansluten(val)

        print("  Ingen aktiv session.")
        print()
        print("   1  Auto-scan alla styrdon")
        print("   2  Anslut till styrdon")
        print("   3  Visa anslutningsinformation")
        print("   0  Avsluta")
        val = fraga("\n  Val")
        if val == "1":
            self.autoscan()
        elif val == "2":
            self._anslut()
        elif val == "3":
            self._visa_anslutningsinfo()
        elif val in ("0", "q", "Q"):
            return False
        elif val:
            print("  Okänt val.")
        return True

    def _utfor_ansluten(self, val: str) -> bool:
        atgarder: dict[str, Callable[[], None]] = {
            "1": self._felkoder,
            "2": self._radera_felkoder,
            "3": self._matgrupper,
            "4": self._grundinstallning,
            "5": self._stalldonstest,
            "6": self._anpassning,
            "7": self._login,
            "8": self._visa_ident,
            "9": self.koppla_ner,
        }
        if val in ("0", "q", "Q"):
            return False
        atgard = atgarder.get(val)
        if atgard is None:
            if val:
                print("  Okänt val.")
            return True
        atgard()
        return True

    def _kontrollera_bakgrundsfel(self) -> None:
        """Rapportera fel som keep-alive-tråden råkat ut för."""
        if self.klient is None:
            return
        fel = self.klient.bakgrundsfel
        if fel is not None and not self.klient.ansluten:
            visa_fel(fel)
            self.klient = None

    # -- anslutning --------------------------------------------------------

    def ny_klient(self) -> KWP1281:
        return KWP1281(
            self.transport,
            timeout=self.timeout,
            init_timeout=self.init_timeout,
            spar=self.spar,
        )

    def anslut(self, adress: int) -> Identifikation:
        """Anslut till ett styrdon och starta keep-alive."""
        self.koppla_ner()
        klient = self.ny_klient()
        print(f"\n  Väcker 0x{adress:02X} {styrdonsnamn(adress)} "
              f"(5-baud-init tar ~2 sekunder) ...")
        ident = klient.anslut(adress)
        klient.starta_keepalive()
        self.klient = klient
        print("  Ansluten.")
        for rad in formatera_ident(ident):
            print(rad)
        return ident

    def _anslut(self) -> None:
        print(rubrik("ANSLUT TILL STYRDON"))
        for adress in AUTOSCAN_ADRESSER:
            print(f"   {adress:02X}  {ADRESSER.get(adress, '')}")
        svar = fraga("\n  Styrdonsadress (hex)", "01")
        try:
            adress = int(svar, 16)
        except ValueError:
            print("  Ogiltig adress.")
            return
        if not 0 <= adress <= 0xFF:
            print("  Adressen måste vara 00–FF.")
            return
        self.anslut(adress)

    def _visa_anslutningsinfo(self) -> None:
        print(rubrik("ANSLUTNING"))
        print(f"  Transport: {self.transport.namn}")
        latens = getattr(self.transport, "latens_ms", None)
        if latens is None:
            print("  FTDI Latency Timer: okänd (kontrollera manuellt, ska vara 1 ms)")
        else:
            status = "OK" if latens <= 2 else "FÖR HÖG – sätt till 1 ms"
            print(f"  FTDI Latency Timer: {latens} ms ({status})")
        print("  Baudrate: 10400, 8N1, halvduplex med eko")

    def _visa_ident(self) -> None:
        klient = self._krav_klient()
        print(rubrik("IDENTIFIKATION"))
        if klient.ident:
            for rad in formatera_ident(klient.ident):
                print(rad)

    def _krav_klient(self) -> KWP1281:
        if self.klient is None or not self.klient.ansluten:
            raise VagdiagFel(
                "Ingen aktiv session.",
                tips="Välj 2 i huvudmenyn och anslut till ett styrdon först.",
            )
        return self.klient

    # -- auto-scan ---------------------------------------------------------

    def autoscan(self) -> None:
        print(rubrik("AUTO-SCAN"))
        print("  Provar alla vanliga styrdonsadresser. De flesta svarar inte –")
        print("  det är normalt i en bil från 1999. Tar ett par minuter.\n")
        self.koppla_ner()

        def visa(post: Skanresultat) -> None:
            if post.svarade:
                antal = len(post.felkoder)
                print(f"   0x{post.adress:02X} {post.namn:<28} SVARAR   "
                      f"{antal} felkod(er)")
            else:
                print(f"   0x{post.adress:02X} {post.namn:<28} tyst")

        resultat = skanna(
            self.transport,
            adresser=AUTOSCAN_ADRESSER,
            forsok=2,
            timeout=0.6,
            init_timeout=1.2,
            paus=0.6,
            aterkoppling=visa,
            spar=self.spar,
        )
        self._skanrapport(resultat)

    def _skanrapport(self, resultat: Sequence[Skanresultat]) -> None:
        """Slutrapport efter auto-scan."""
        svarande = [r for r in resultat if r.svarade]
        totalt = sum(len(r.felkoder) for r in svarande)
        print(rubrik("SLUTRAPPORT"))
        print(f"  Tidpunkt: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"  {len(svarande)} av {len(resultat)} adresser svarade, "
              f"{totalt} felkod(er) totalt.\n")
        for post in svarande:
            assert post.ident is not None
            print(f"  0x{post.adress:02X} {post.namn}")
            print(f"       {post.ident.delnummer} {post.ident.komponent}")
            if post.fel:
                print(f"       ({post.fel})")
            for rad in formatera_felkoder(post.felkoder):
                print("     " + rad)
            print()
        if not svarande:
            print("  Inget styrdon svarade alls. Kontrollera tändning, kabel")
            print("  och att FTDI Latency Timer är 1 ms.")

    # -- felkoder ----------------------------------------------------------

    def _felkoder(self) -> None:
        klient = self._krav_klient()
        print(rubrik("FELKODER"))
        koder = klient.las_felkoder()
        for rad in formatera_felkoder(koder):
            print(rad)

    def _radera_felkoder(self) -> None:
        klient = self._krav_klient()
        print(rubrik("RADERA FELKODER"))
        varning([
            "Felkodsminnet raderas permanent. Läs och anteckna koderna först –",
            "de går inte att få tillbaka.",
            "",
            "Radera aldrig innan du förstått vad koden betyder. En kod som",
            "kommer tillbaka efter provkörning är den intressanta.",
        ])
        if not fraga_exakt("  Radera felkodsminnet?", "JA"):
            return
        klient.radera_felkoder()
        print("  Felkodsminnet raderat.")
        kvar = klient.las_felkoder()
        if kvar:
            print("  OBS: följande koder kom tillbaka direkt (aktiva fel):")
            for rad in formatera_felkoder(kvar):
                print(rad)

    # -- mätgrupper --------------------------------------------------------

    def _fraga_grupper(self, standard: str = "3 4 11") -> list[int]:
        svar = fraga("  Grupper (mellanslagsseparerade, 1-255)", standard)
        grupper: list[int] = []
        for bit in svar.replace(",", " ").split():
            try:
                nummer = int(bit, 10)
            except ValueError:
                print(f"  Hoppar över '{bit}' – inte ett tal.")
                continue
            if 1 <= nummer <= 255:
                grupper.append(nummer)
            else:
                print(f"  Hoppar över {nummer} – utanför 1-255.")
        return grupper

    def _matgrupper(self) -> None:
        klient = self._krav_klient()
        print(rubrik("MÄTGRUPPER LIVE"))
        grupper = self._fraga_grupper()
        if not grupper:
            print("  Inga giltiga grupper angivna.")
            return
        loggfil = fraga("  CSV-loggfil (tom = ingen loggning)")
        self.live(klient, grupper, loggfil or None)

    def live(
        self,
        klient: KWP1281,
        grupper: Sequence[int],
        loggfil: str | None = None,
        grundinstallning: bool = False,
    ) -> None:
        """Liveavläsning av mätgrupper med valfri CSV-loggning."""
        adress = klient.adress or 0x01
        logg: CsvLogg | None = None
        if loggfil:
            logg = CsvLogg(
                loggfil, grupper, adress,
                avgransare=self.avgransare, decimalkomma=self.decimalkomma,
            )
        rensa_skarm()
        start = time.monotonic()
        varv = 0
        try:
            with Tangentbord() as tangentbord:
                while True:
                    matningar: dict[int, list[Matvarde]] = {}
                    for grupp in grupper:
                        matningar[grupp] = (
                            klient.grundinstallning(grupp)
                            if grundinstallning
                            else klient.las_matgrupp(grupp)
                        )
                    varv += 1
                    if logg is not None:
                        logg.logga(matningar)
                    rita(self._liverader(
                        adress, grupper, matningar, logg, start, varv, grundinstallning
                    ))
                    tangent = tangentbord.las()
                    if tangent in ("q", "Q", "\x03", "\x1b"):
                        break
                    if tangent in ("l", "L"):
                        logg = self._vaxla_logg(logg, grupper, adress)
                    if self.intervall:
                        time.sleep(self.intervall)
        finally:
            if logg is not None:
                logg.stang()
                print(f"\n  Logg sparad: {logg.filnamn} ({logg.rader} rader)")
            print()

    def _vaxla_logg(
        self, logg: CsvLogg | None, grupper: Sequence[int], adress: int
    ) -> CsvLogg | None:
        """Starta eller stoppa loggning med tangenten l."""
        if logg is not None:
            logg.stang()
            return None
        namn = f"vagdiag_{datetime.now():%Y%m%d_%H%M%S}.csv"
        return CsvLogg(
            namn, grupper, adress,
            avgransare=self.avgransare, decimalkomma=self.decimalkomma,
        )

    def _liverader(
        self,
        adress: int,
        grupper: Sequence[int],
        matningar: dict[int, list[Matvarde]],
        logg: CsvLogg | None,
        start: float,
        varv: int,
        grundinstallning: bool,
    ) -> list[str]:
        """Bygg skärmbilden för liveläget."""
        gatt = time.monotonic() - start
        lage = "GRUNDINSTÄLLNING" if grundinstallning else "MÄTGRUPPER"
        loggtext = (
            f"logg: {logg.filnamn.name} ({logg.rader} rader)"
            if logg is not None else "logg: av"
        )
        rader = [
            f" VAGDIAG {lage}  0x{adress:02X} {styrdonsnamn(adress)}",
            f" {datetime.now():%H:%M:%S}   {gatt:6.1f} s   varv {varv:<6d} "
            f"{varv / gatt if gatt else 0:4.1f} varv/s   {loggtext}",
            linje(),
        ]
        for grupp in grupper:
            rader.extend(formatera_matvarden(grupp, matningar.get(grupp, []), adress))
            rader.append("")
        rader.append(linje())
        rader.append(" [q] tillbaka till menyn    [l] starta/stoppa loggning")
        rader.append(" Värden märkta ? kommer från rekonstruerade formler –"
                     " jämför mot VCDS.")
        return rader

    # -- grundinställning --------------------------------------------------

    def _grundinstallning(self) -> None:
        klient = self._krav_klient()
        print(rubrik("GRUNDINSTÄLLNING"))
        varning([
            "Grundinställning är inte samma sak som att läsa mätvärden.",
            "Styrdonet kan starta ställdon, nollställa adaptioner och köra",
            "motorn på egen hand.",
            "",
            "Kör bara grupper du vet vad de gör. Följ alltid reparations-",
            "handboken för din motorkod – fel grupp kan ge en bil som inte",
            "startar. Handbromsen ska vara åtdragen och växeln i neutral.",
        ])
        if not fraga_exakt("  Fortsätta till grundinställning?", "JA"):
            return
        grupper = self._fraga_grupper("3")
        if not grupper:
            return
        loggfil = fraga("  CSV-loggfil (tom = ingen loggning)")
        self.live(klient, grupper, loggfil or None, grundinstallning=True)

    # -- ställdonstest -----------------------------------------------------

    def _stalldonstest(self) -> None:
        klient = self._krav_klient()
        print(rubrik("STÄLLDONSTEST"))
        varning([
            "MOTORN SKA VARA AVSTÄNGD, tändningen PÅ.",
            "",
            "Styrdonet aktiverar ett ställdon i taget: ventiler, reläer och",
            "lampor klickar och rör sig. Håll händer, verktyg och kläder borta",
            "från kylarfläkt, remmar och rörliga delar.",
            "",
            "Sekvensen går bara framåt och kan inte backas. Avbryt med q.",
        ])
        if not fraga_exakt("  Starta ställdonstestet?", "JA"):
            return
        nummer = 0
        while True:
            stalldon = klient.stalldonstest_nasta()
            if stalldon is None:
                print("\n  Sekvensen är slut – alla ställdon genomgångna.")
                return
            nummer += 1
            print(f"\n  {nummer:2d}. {stalldon.namn}")
            print(f"      komponentkod 0x{stalldon.kod:04X}")
            svar = fraga("      [Enter] nästa, [q] avbryt")
            if svar.lower().startswith("q"):
                print("\n  Avbrutet. Slå av tändningen för att lämna testläget.")
                return

    # -- anpassning --------------------------------------------------------

    def _anpassning(self) -> None:
        klient = self._krav_klient()
        adress = klient.adress or 0
        print(rubrik("ANPASSNING"))
        varning([
            "Anpassning ändrar värden i styrdonets permanenta minne.",
            "Läs alltid av det gamla värdet och anteckna det innan du sparar.",
            "Testa alltid ett värde (0x22) innan du sparar det (0x2A).",
        ])
        if adress in KANSLIGA_ADRESSER:
            varning([
                f"EXTRA VARNING: 0x{adress:02X} {styrdonsnamn(adress)}.",
                "",
                "Felaktig anpassning här kan aktivera startspärren så att bilen",
                "inte startar, eller förstöra mätarställning och servicedata.",
                "Sådant kan kräva verkstad med online-kodning för att rätta till.",
                "",
                "Fortsätt bara om du vet exakt vad kanalen gör.",
            ])
            if not fraga_exakt("  Fortsätta ändå?", "JAG FÖRSTÅR"):
                return

        kanal = fraga_heltal("  Kanal (0-255)", 1, 0, 255)
        if kanal is None:
            return
        anpassning = klient.las_anpassning(kanal)
        print(f"\n  Kanal {kanal}: nuvarande värde = {anpassning.varde}")
        for i, varde in enumerate(anpassning.matvarden, start=1):
            print(f"      mätvärde {i}: {varde.text}")

        nytt = fraga_heltal("\n  Nytt värde att TESTA (tom = avbryt)", None)
        if nytt is None:
            print("  Avbrutet – inget ändrat.")
            return
        test = klient.testa_anpassning(kanal, nytt)
        print(f"\n  Testar {test.varde} (ej sparat).")
        for i, varde in enumerate(test.matvarden, start=1):
            print(f"      mätvärde {i}: {varde.text}")
        print(f"\n  Gammalt värde: {anpassning.varde}   Nytt värde: {test.varde}")

        if not fraga_exakt("  Spara permanent?", "SPARA"):
            print(f"  Inget sparat. Gammalt värde {anpassning.varde} gäller "
                  "efter att tändningen slagits av.")
            return
        sparad = klient.spara_anpassning(kanal, nytt)
        print(f"  Sparat. Kanal {kanal} = {sparad.varde} "
              f"(var {anpassning.varde}).")

    # -- login -------------------------------------------------------------

    def _login(self) -> None:
        klient = self._krav_klient()
        print(rubrik("LOGIN"))
        print("  Femsiffrig login-kod krävs för vissa anpassningar och tester.")
        kod = fraga_heltal("  Login-kod (0-65535, tom = avbryt)", None)
        if kod is None:
            return
        if klient.login(kod):
            print(f"  Login {kod:05d} accepterad.")
        else:
            print(f"  Login {kod:05d} nekades av styrdonet.")
