"""tkinter-instrumentpanel för VAGDIAG (fas 2).

Trådmodell
----------
All seriekommunikation sker i :class:`Diagnostrad`. Den tråden rör **aldrig**
tkinter. Resultat läggs i en :class:`queue.Queue` som GUI-tråden tömmer via
``after()``. Kommandon åt andra hållet (läs felkoder, radera, byt grupper,
starta/stoppa loggning) går genom en andra kö. Därför kan GUI:t aldrig frysa
av en långsam eller död K-line, och serietråden kan aldrig krascha tkinter.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk
from typing import Sequence

from .felkoder import Felkod
from .formler import Matvarde
from .kwp1281 import Identifikation
from .loggning import CsvLogg
from .meny import Meny
from .styrdon import etikett, gruppnamn, styrdonsnamn
from .undantag import VagdiagFel

__all__ = ["kor_gui", "Instrumentpanel", "Diagnostrad"]

# Färger – mörk panel är lättare att läsa i en bil.
BAKGRUND = "#12141a"
PANEL = "#1b1f2a"
TEXT = "#e6e9f0"
DAMPAD = "#8b93a7"
AR_FARG = "#4da3ff"      # ÄR-värde
BOR_FARG = "#ffb454"     # BÖR-värde
LARM = "#ff5f56"
OK_FARG = "#3ddc84"

SERIEFARGER = [
    "#4da3ff", "#ffb454", "#3ddc84", "#ff5f56",
    "#c792ea", "#89ddff", "#f78c6c", "#a3be8c",
]

#: Hur många sekunder den rullande grafen visar.
GRAFFONSTER = 60.0


# ---------------------------------------------------------------------------
# Meddelanden mellan trådarna
# ---------------------------------------------------------------------------


@dataclass
class Matning:
    """Ett avläsningsvarv."""

    tid: float
    varden: dict[int, list[Matvarde]] = field(default_factory=dict)


@dataclass
class Status:
    """Statusrad till GUI:t."""

    text: str
    fel: bool = False


# ---------------------------------------------------------------------------
# Serietråden
# ---------------------------------------------------------------------------


class Diagnostrad(threading.Thread):
    """Sköter all ECU-kommunikation. Får aldrig röra tkinter."""

    def __init__(
        self,
        meny: Meny,
        adress: int,
        grupper: Sequence[int],
        ut: queue.Queue[object],
    ) -> None:
        super().__init__(name="vagdiag-serie", daemon=True)
        self.meny = meny
        self.adress = adress
        self.grupper = list(grupper)
        self.ut = ut
        self.inkommande: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stopp = threading.Event()
        self._logg: CsvLogg | None = None
        self._pausad = False

    # -- kommandon från GUI-tråden ----------------------------------------

    def kommando(self, namn: str, data: object = None) -> None:
        """Köa ett kommando till serietråden."""
        self.inkommande.put((namn, data))

    def stoppa(self) -> None:
        """Avsluta tråden och vänta in den."""
        self._stopp.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=4.0)

    # -- huvudloop ---------------------------------------------------------

    def run(self) -> None:
        klient = None
        try:
            self.ut.put(Status(
                f"Väcker 0x{self.adress:02X} {styrdonsnamn(self.adress)} ..."
            ))
            klient = self.meny.ny_klient()
            ident = klient.anslut(self.adress)
            klient.starta_keepalive()
            self.ut.put(ident)
            self.ut.put(Status(f"Ansluten: {ident.sammanfattning()}"))

            while not self._stopp.is_set():
                self._hantera_kommandon(klient)
                if self._stopp.is_set():
                    break
                if self._pausad or not self.grupper:
                    klient.keep_alive()
                    time.sleep(0.2)
                    continue
                matning = Matning(tid=time.monotonic())
                for grupp in self.grupper:
                    matning.varden[grupp] = klient.las_matgrupp(grupp)
                if self._logg is not None:
                    self._logg.logga(matning.varden)
                self.ut.put(matning)
                if self.meny.intervall:
                    time.sleep(self.meny.intervall)
        except VagdiagFel as fel:
            self.ut.put(fel)
        except Exception as fel:  # oväntat – visa ändå något begripligt
            self.ut.put(VagdiagFel(f"Oväntat fel i serietråden: {fel!r}"))
        finally:
            self._stang_logg()
            if klient is not None:
                try:
                    klient.koppla_ner()
                except VagdiagFel:
                    pass
            self.ut.put(Status("Frånkopplad."))

    def _hantera_kommandon(self, klient) -> None:
        """Töm kommandokön."""
        while True:
            try:
                namn, data = self.inkommande.get_nowait()
            except queue.Empty:
                return
            if namn == "grupper":
                self.grupper = list(data)  # type: ignore[arg-type]
                self._byt_logg_grupper()
            elif namn == "paus":
                self._pausad = bool(data)
            elif namn == "las_felkoder":
                self.ut.put(("felkoder", klient.las_felkoder()))
            elif namn == "radera_felkoder":
                klient.radera_felkoder()
                self.ut.put(Status("Felkodsminnet raderat."))
                self.ut.put(("felkoder", klient.las_felkoder()))
            elif namn == "logg_start":
                self._starta_logg(str(data))
            elif namn == "logg_stopp":
                self._stang_logg()
            elif namn == "stopp":
                self._stopp.set()
                return

    # -- loggning ----------------------------------------------------------

    def _starta_logg(self, filnamn: str) -> None:
        self._stang_logg()
        self._logg = CsvLogg(
            filnamn, self.grupper, self.adress,
            avgransare=self.meny.avgransare,
            decimalkomma=self.meny.decimalkomma,
        )
        self.ut.put(Status(f"Loggar till {filnamn}"))

    def _byt_logg_grupper(self) -> None:
        """Grupperna ändrades – en pågående logg måste börja på ny fil."""
        if self._logg is None:
            return
        gammal = self._logg.filnamn
        self._stang_logg()
        self.ut.put(Status(
            f"Grupperna ändrades – loggen {gammal.name} stängdes. Starta en ny."
        ))

    def _stang_logg(self) -> None:
        if self._logg is not None:
            namn, rader = self._logg.filnamn, self._logg.rader
            self._logg.stang()
            self._logg = None
            self.ut.put(Status(f"Logg sparad: {namn} ({rader} rader)"))

    @property
    def loggar(self) -> bool:
        """True om loggning pågår."""
        return self._logg is not None


# ---------------------------------------------------------------------------
# Mätare
# ---------------------------------------------------------------------------


class Matare(tk.Canvas):
    """Rund mätartavla med visare för ÄR-värde och (valfritt) BÖR-värde."""

    def __init__(
        self,
        master: tk.Misc,
        rubrik: str,
        minvarde: float,
        maxvarde: float,
        enhet: str,
        storlek: int = 190,
    ) -> None:
        super().__init__(
            master, width=storlek, height=storlek,
            bg=PANEL, highlightthickness=0,
        )
        self.rubrik = rubrik
        self.minvarde = minvarde
        self.maxvarde = maxvarde
        self.enhet = enhet
        self.storlek = storlek
        self._rita_skala()
        self._visare_ar: int | None = None
        self._visare_bor: int | None = None
        self._text_ar = self.create_text(
            storlek / 2, storlek * 0.70, text="–",
            fill=TEXT, font=("Segoe UI", 15, "bold"),
        )
        self._text_bor = self.create_text(
            storlek / 2, storlek * 0.83, text="",
            fill=BOR_FARG, font=("Segoe UI", 9),
        )

    # -- ritning -----------------------------------------------------------

    def _rita_skala(self) -> None:
        s = self.storlek
        marginal = s * 0.10
        self.create_arc(
            marginal, marginal, s - marginal, s - marginal,
            start=-45, extent=270, style=tk.ARC, outline="#2c3242", width=10,
        )
        for i in range(11):
            andel = i / 10
            vinkel = math.radians(225 - 270 * andel)
            r_yttre = s / 2 - marginal * 0.7
            r_inre = r_yttre - (s * 0.045 if i % 5 == 0 else s * 0.025)
            mitt = s / 2
            self.create_line(
                mitt + r_inre * math.cos(vinkel), mitt - r_inre * math.sin(vinkel),
                mitt + r_yttre * math.cos(vinkel), mitt - r_yttre * math.sin(vinkel),
                fill=DAMPAD, width=2 if i % 5 == 0 else 1,
            )
        self.create_text(
            s / 2, s * 0.20, text=self.rubrik, fill=DAMPAD,
            font=("Segoe UI", 10, "bold"),
        )
        self.create_text(
            s / 2, s * 0.93, text=self.enhet, fill=DAMPAD, font=("Segoe UI", 8),
        )

    def _visarpunkt(self, varde: float, langd: float) -> tuple[float, float, float, float]:
        omfang = self.maxvarde - self.minvarde or 1.0
        andel = min(1.0, max(0.0, (varde - self.minvarde) / omfang))
        vinkel = math.radians(225 - 270 * andel)
        mitt = self.storlek / 2
        r = self.storlek / 2 * langd
        return mitt, mitt, mitt + r * math.cos(vinkel), mitt - r * math.sin(vinkel)

    def uppdatera(self, ar: float | None, bor: float | None = None) -> None:
        """Rita om visarna. None döljer respektive visare."""
        for handtag in (self._visare_bor, self._visare_ar):
            if handtag is not None:
                self.delete(handtag)
        self._visare_bor = None
        self._visare_ar = None

        if bor is not None:
            self._visare_bor = self.create_line(
                *self._visarpunkt(bor, 0.66), fill=BOR_FARG, width=3,
            )
            self.itemconfigure(self._text_bor, text=f"BÖR {bor:,.0f}".replace(",", " "))
        else:
            self.itemconfigure(self._text_bor, text="")

        if ar is not None:
            self._visare_ar = self.create_line(
                *self._visarpunkt(ar, 0.72), fill=AR_FARG, width=4,
            )
            self.itemconfigure(self._text_ar, text=f"{ar:,.0f}".replace(",", " "))
        else:
            self.itemconfigure(self._text_ar, text="–")


# ---------------------------------------------------------------------------
# Rullande graf
# ---------------------------------------------------------------------------


class Rullgraf(tk.Canvas):
    """Realtidsgraf över de senaste ``GRAFFONSTER`` sekunderna.

    Varje serie skalas mot sitt eget min/max i fönstret, eftersom varvtal och
    laddtryck inte går att lägga på samma axel. Aktuellt värde står i förklaringen.
    """

    def __init__(self, master: tk.Misc, hojd: int = 220) -> None:
        super().__init__(master, height=hojd, bg=PANEL, highlightthickness=0)
        self._punkter: list[tuple[float, dict[str, tuple[float, str]]]] = []
        self.bind("<Configure>", lambda _h: self.rita())

    def lagg_till(self, tid: float, serier: dict[str, tuple[float, str]]) -> None:
        """Lägg till ett mätvarv: {etikett: (värde, enhet)}."""
        self._punkter.append((tid, serier))
        grans = tid - GRAFFONSTER
        while self._punkter and self._punkter[0][0] < grans:
            self._punkter.pop(0)

    def rensa(self) -> None:
        """Töm grafen."""
        self._punkter.clear()
        self.rita()

    def rita(self) -> None:
        """Rita om hela grafen."""
        self.delete("all")
        bredd = max(self.winfo_width(), 10)
        hojd = max(self.winfo_height(), 10)
        vanster, hoger, uppe, nere = 8, bredd - 150, 12, hojd - 20

        for i in range(5):
            y = uppe + (nere - uppe) * i / 4
            self.create_line(vanster, y, hoger, y, fill="#252b38")
        self.create_text(
            vanster + 4, nere + 10, anchor="w",
            text=f"senaste {GRAFFONSTER:.0f} s", fill=DAMPAD, font=("Segoe UI", 8),
        )
        if len(self._punkter) < 2:
            self.create_text(
                (vanster + hoger) / 2, (uppe + nere) / 2,
                text="samlar mätvärden ...", fill=DAMPAD, font=("Segoe UI", 10),
            )
            return

        t_slut = self._punkter[-1][0]
        t_start = t_slut - GRAFFONSTER
        namn = list(self._punkter[-1][1].keys())

        for index, serienamn in enumerate(namn):
            farg = SERIEFARGER[index % len(SERIEFARGER)]
            varden = [
                (t, s[serienamn][0]) for t, s in self._punkter if serienamn in s
            ]
            if len(varden) < 2:
                continue
            lagsta = min(v for _t, v in varden)
            hogsta = max(v for _t, v in varden)
            omfang = (hogsta - lagsta) or 1.0
            koordinater: list[float] = []
            for t, v in varden:
                x = vanster + (hoger - vanster) * (t - t_start) / GRAFFONSTER
                y = nere - (nere - uppe) * (v - lagsta) / omfang
                koordinater.extend((max(vanster, x), y))
            self.create_line(*koordinater, fill=farg, width=2, smooth=True)

            aktuellt, enhet = self._punkter[-1][1][serienamn]
            y_text = uppe + index * 16
            self.create_line(hoger + 8, y_text, hoger + 26, y_text, fill=farg, width=3)
            self.create_text(
                hoger + 32, y_text, anchor="w",
                text=f"{serienamn}  {aktuellt:.1f} {enhet}",
                fill=TEXT, font=("Segoe UI", 8),
            )


# ---------------------------------------------------------------------------
# Huvudfönstret
# ---------------------------------------------------------------------------


def _hitta_par(
    matning: Matning, enhet: str
) -> tuple[float | None, float | None]:
    """Leta upp (BÖR, ÄR) för en enhet, t.ex. mbar eller mg/slag.

    VAG-blocken lägger BÖR på position 2 och ÄR på position 3. Finns bara ett
    värde med enheten tolkas det som ÄR.
    """
    for varden in matning.varden.values():
        traffar = [(i, v) for i, v in enumerate(varden, start=1)
                   if v.enhet == enhet and v.ar_tal]
        if len(traffar) >= 2:
            return traffar[0][1].tal, traffar[1][1].tal
        if traffar:
            return None, traffar[0][1].tal
    return None, None


def _hitta_enkel(matning: Matning, enhet: str) -> float | None:
    """Första numeriska värdet med angiven enhet."""
    for varden in matning.varden.values():
        for varde in varden:
            if varde.enhet == enhet and varde.ar_tal:
                return varde.tal
    return None


class Instrumentpanel(tk.Tk):
    """Huvudfönstret: mätartavlor, realtidsgraf, loggning och felkoder."""

    def __init__(self, meny: Meny, adress: int = 0x01,
                 grupper: Sequence[int] = (3, 11)) -> None:
        super().__init__()
        self.meny = meny
        self.adress = adress
        self.title("VAGDIAG – instrumentpanel")
        self.geometry("1080x760")
        self.configure(bg=BAKGRUND)
        self.protocol("WM_DELETE_WINDOW", self._avsluta)

        self._ko: queue.Queue[object] = queue.Queue()
        self._trad: Diagnostrad | None = None
        self._start = time.monotonic()
        self._loggar = False
        self._senaste: Matning | None = None

        self._bygg_gransnitt(grupper)
        self._starta_trad(grupper)
        self.after(50, self._tom_ko)

    # -- gränssnitt --------------------------------------------------------

    def _bygg_gransnitt(self, grupper: Sequence[int]) -> None:
        stil = ttk.Style(self)
        try:
            stil.theme_use("clam")
        except tk.TclError:  # pragma: no cover - beror på plattform
            pass
        stil.configure("TNotebook", background=BAKGRUND, borderwidth=0)
        stil.configure("TNotebook.Tab", background=PANEL, foreground=TEXT, padding=(14, 6))
        stil.map("TNotebook.Tab", background=[("selected", "#2a3142")])
        stil.configure("TFrame", background=BAKGRUND)

        flikar = ttk.Notebook(self)
        flikar.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        matflik = ttk.Frame(flikar)
        felflik = ttk.Frame(flikar)
        flikar.add(matflik, text="  Mätvärden  ")
        flikar.add(felflik, text="  Felkoder  ")

        self._bygg_matflik(matflik, grupper)
        self._bygg_felflik(felflik)

        self.statusrad = tk.Label(
            self, text="Startar ...", bg=BAKGRUND, fg=DAMPAD,
            anchor="w", font=("Segoe UI", 9),
        )
        self.statusrad.pack(fill="x", padx=12, pady=6)

    def _bygg_matflik(self, ram: ttk.Frame, grupper: Sequence[int]) -> None:
        matarrad = tk.Frame(ram, bg=BAKGRUND)
        matarrad.pack(fill="x", pady=(10, 4))

        self.matare_varv = Matare(matarrad, "VARVTAL", 0, 5000, "1/min")
        self.matare_ladd = Matare(matarrad, "LADDTRYCK", 800, 2400, "mbar")
        self.matare_maf = Matare(matarrad, "LUFTMASSA", 0, 1000, "mg/slag")
        for matare in (self.matare_varv, self.matare_ladd, self.matare_maf):
            matare.pack(side="left", padx=10)

        varden = tk.Frame(matarrad, bg=PANEL)
        varden.pack(side="left", fill="both", expand=True, padx=10)
        self.varderuta = tk.Text(
            varden, bg=PANEL, fg=TEXT, bd=0, height=11,
            font=("Consolas", 9), state="disabled", wrap="none",
        )
        self.varderuta.pack(fill="both", expand=True, padx=8, pady=8)

        self.graf = Rullgraf(ram)
        self.graf.pack(fill="both", expand=True, padx=12, pady=8)

        kontroller = tk.Frame(ram, bg=BAKGRUND)
        kontroller.pack(fill="x", padx=12, pady=(0, 10))

        tk.Label(kontroller, text="Grupper:", bg=BAKGRUND, fg=TEXT).pack(side="left")
        self.gruppfalt = tk.Entry(kontroller, width=18, bg=PANEL, fg=TEXT,
                                  insertbackground=TEXT, relief="flat")
        self.gruppfalt.insert(0, " ".join(str(g) for g in grupper))
        self.gruppfalt.pack(side="left", padx=(6, 4))
        tk.Button(kontroller, text="Använd", command=self._byt_grupper,
                  bg=PANEL, fg=TEXT, relief="flat", padx=10).pack(side="left")

        self.loggknapp = tk.Button(
            kontroller, text="● Starta loggning", command=self._vaxla_logg,
            bg=PANEL, fg=OK_FARG, relief="flat", padx=14,
        )
        self.loggknapp.pack(side="right")
        self.pausknapp = tk.Button(
            kontroller, text="Pausa", command=self._vaxla_paus,
            bg=PANEL, fg=TEXT, relief="flat", padx=14,
        )
        self.pausknapp.pack(side="right", padx=8)

    def _bygg_felflik(self, ram: ttk.Frame) -> None:
        knappar = tk.Frame(ram, bg=BAKGRUND)
        knappar.pack(fill="x", padx=12, pady=10)
        tk.Button(knappar, text="Läs felkoder", command=self._las_felkoder,
                  bg=PANEL, fg=TEXT, relief="flat", padx=14).pack(side="left")
        tk.Button(knappar, text="Radera felkoder", command=self._radera_felkoder,
                  bg=PANEL, fg=LARM, relief="flat", padx=14).pack(side="left", padx=8)

        self.felruta = tk.Text(
            ram, bg=PANEL, fg=TEXT, bd=0, font=("Consolas", 10),
            state="disabled", wrap="word",
        )
        self.felruta.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._skriv_felruta("Tryck på \"Läs felkoder\".")

    # -- trådstyrning ------------------------------------------------------

    def _starta_trad(self, grupper: Sequence[int]) -> None:
        self._trad = Diagnostrad(self.meny, self.adress, grupper, self._ko)
        self._trad.start()

    def _tom_ko(self) -> None:
        """Hämta meddelanden från serietråden. Körs i GUI-tråden via after()."""
        try:
            while True:
                self._hantera(self._ko.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._tom_ko)

    def _hantera(self, meddelande: object) -> None:
        if isinstance(meddelande, Matning):
            self._visa_matning(meddelande)
        elif isinstance(meddelande, Status):
            self._status(meddelande.text, meddelande.fel)
        elif isinstance(meddelande, Identifikation):
            self.title(f"VAGDIAG – {meddelande.namn} {meddelande.delnummer}")
        elif isinstance(meddelande, VagdiagFel):
            self._visa_fel(meddelande)
        elif isinstance(meddelande, tuple) and meddelande[0] == "felkoder":
            self._visa_felkoder(meddelande[1])

    def _status(self, text: str, fel: bool = False) -> None:
        self.statusrad.configure(text=text, fg=LARM if fel else DAMPAD)

    def _visa_fel(self, fel: VagdiagFel) -> None:
        self._status(f"FEL: {fel.meddelande}", fel=True)
        messagebox.showerror(
            "Kommunikationsfel",
            f"{fel.meddelande}\n\n{fel.tips or ''}",
            parent=self,
        )

    # -- presentation ------------------------------------------------------

    def _visa_matning(self, matning: Matning) -> None:
        self._senaste = matning
        varv = _hitta_enkel(matning, "1/min")
        ladd_bor, ladd_ar = _hitta_par(matning, "mbar")
        maf_bor, maf_ar = _hitta_par(matning, "mg/slag")

        self.matare_varv.uppdatera(varv)
        self.matare_ladd.uppdatera(ladd_ar, ladd_bor)
        self.matare_maf.uppdatera(maf_ar, maf_bor)

        serier: dict[str, tuple[float, str]] = {}
        for grupp, varden in matning.varden.items():
            for position, varde in enumerate(varden, start=1):
                if varde.ar_tal and varde.tal is not None:
                    namn = f"{grupp:03d}.{position} {etikett(grupp, position, self.adress)}"
                    serier[namn[:34]] = (varde.tal, varde.enhet)
        self.graf.lagg_till(matning.tid, dict(list(serier.items())[:8]))
        self.graf.rita()
        self._skriv_varden(matning)

    def _skriv_varden(self, matning: Matning) -> None:
        rader: list[str] = []
        for grupp, varden in matning.varden.items():
            rader.append(f"Grupp {grupp:03d}  {gruppnamn(grupp, self.adress)}")
            if not varden:
                rader.append("   (gruppen finns inte i styrdonet)")
            for position, varde in enumerate(varden, start=1):
                namn = etikett(grupp, position, self.adress)[:26]
                rader.append(f"   {position}  {namn:<26} {varde.text:>18}")
            rader.append("")
        self.varderuta.configure(state="normal")
        self.varderuta.delete("1.0", "end")
        self.varderuta.insert("1.0", "\n".join(rader))
        self.varderuta.configure(state="disabled")

    def _skriv_felruta(self, text: str) -> None:
        self.felruta.configure(state="normal")
        self.felruta.delete("1.0", "end")
        self.felruta.insert("1.0", text)
        self.felruta.configure(state="disabled")

    def _visa_felkoder(self, koder: Sequence[Felkod]) -> None:
        self._status(f"{len(koder)} felkod(er) lästa.")
        if not koder:
            self._skriv_felruta("Inga felkoder lagrade.")
            return
        rader = [f"{len(koder)} felkod(er):", ""]
        for kod in koder:
            markor = "[SP]" if kod.sporadisk else "    "
            rader.append(f"{kod.nummer} {markor} {kod.text}")
            rader.append(f"            {kod.statustext}")
            rader.append("")
        self._skriv_felruta("\n".join(rader))

    # -- knappar -----------------------------------------------------------

    def _byt_grupper(self) -> None:
        grupper: list[int] = []
        for bit in self.gruppfalt.get().replace(",", " ").split():
            try:
                nummer = int(bit)
            except ValueError:
                continue
            if 1 <= nummer <= 255:
                grupper.append(nummer)
        if not grupper:
            messagebox.showwarning(
                "Grupper", "Ange minst en grupp mellan 1 och 255.", parent=self
            )
            return
        self.graf.rensa()
        if self._trad:
            self._trad.kommando("grupper", grupper)
        self._status(f"Läser grupp {', '.join(f'{g:03d}' for g in grupper)}")

    def _vaxla_paus(self) -> None:
        if not self._trad:
            return
        pausad = self.pausknapp.cget("text") == "Pausa"
        self._trad.kommando("paus", pausad)
        self.pausknapp.configure(text="Fortsätt" if pausad else "Pausa")

    def _vaxla_logg(self) -> None:
        if not self._trad:
            return
        if self._loggar:
            self._trad.kommando("logg_stopp")
            self._loggar = False
            self.loggknapp.configure(text="● Starta loggning", fg=OK_FARG)
            return
        filnamn = f"vagdiag_{datetime.now():%Y%m%d_%H%M%S}.csv"
        self._trad.kommando("logg_start", filnamn)
        self._loggar = True
        self.loggknapp.configure(text="■ Stoppa loggning", fg=LARM)

    def _las_felkoder(self) -> None:
        if self._trad:
            self._trad.kommando("las_felkoder")
            self._status("Läser felkoder ...")

    def _radera_felkoder(self) -> None:
        if not self._trad:
            return
        svar = simpledialog.askstring(
            "Radera felkoder",
            "Felkodsminnet raderas permanent och går inte att få tillbaka.\n"
            "Läs och anteckna koderna först.\n\n"
            "Skriv JA för att radera:",
            parent=self,
        )
        if svar != "JA":
            self._status("Radering avbruten – inget ändrades.")
            return
        self._trad.kommando("radera_felkoder")

    # -- avslut ------------------------------------------------------------

    def _avsluta(self) -> None:
        if self._trad:
            self._trad.kommando("stopp")
            self._trad.stoppa()
        self.destroy()


def kor_gui(meny: Meny, adress: int = 0x01,
            grupper: Sequence[int] = (3, 11)) -> int:
    """Starta instrumentpanelen. Returnerar exitkod."""
    try:
        panel = Instrumentpanel(meny, adress, grupper)
    except tk.TclError as fel:  # pragma: no cover - kräver grafisk miljö
        print(f"Kunde inte öppna ett fönster: {fel}")
        print("Kör terminalläget i stället: python -m vagdiag <port>")
        return 2
    panel.mainloop()
    return 0
