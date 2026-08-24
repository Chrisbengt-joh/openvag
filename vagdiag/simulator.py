"""Virtuell KWP1281-ECU för utveckling och test utan bil.

Simulatorn kopplas in via :func:`vagdiag.transport.skapa_lankat_par`, alltså
direkt i minnet utan com0com, pty:er eller riktiga serieportar. Den kör exakt
samma block- och kvittenslogik som klienten (:class:`vagdiag.kwp1281.KWPKanal`),
så hela protokollstacken testas på riktigt.

Simulatorn föreställer ett motorstyrdon till en 1.9 TDI med VP37 och serverar

* identifikationsblock ``1Z9906019 TDI SIMULATOR``,
* mätvärdesblock 1–13 med värden som varierar realistiskt över tid,
* felkoderna 17965 (sporadisk) och 00560,
* en ställdonssekvens och några anpassningskanaler.

Dessutom kan fel injiceras (tappad kvittens, tystnad, korrupt block) för att
verifiera att klienten hanterar trasiga sessioner snyggt.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import formler
from .kwp1281 import ETX, Block, Blocktitel, KWPKanal
from .transport import Minnestransport, skapa_lankat_par
from .undantag import KWPTimeout, VagdiagFel

__all__ = [
    "Felinjektion",
    "SimuleradECU",
    "starta_simulator",
    "SIM_FELKODER",
    "SIM_STALLDON",
]

#: Nyckelbytes som simulatorn skickar efter 5-baud-väckningen.
KEYBYTE_1 = 0x01
KEYBYTE_2 = 0x8A

#: Felkoder i det simulerade minnet: (kod, status).
SIM_FELKODER: list[tuple[int, int]] = [
    (17965, 0xAA),  # laddtrycksreglering positiv avvikelse, sporadisk
    (560, 0x2A),    # EGR reglergräns, statisk
]

#: Ställdonssekvens som simulatorn stegar igenom.
SIM_STALLDON: list[int] = [0x0102, 0x0103, 0x0101, 0x010C]

#: Startvärden för anpassningskanaler.
SIM_ANPASSNING: dict[int, int] = {0: 0, 1: 32768, 2: 100, 3: 15000, 4: 1}

#: Login-kod som simulatorn accepterar.
SIM_LOGIN = 11463


@dataclass
class Felinjektion:
    """Engångsflaggor för att provocera fram fel hos klienten.

    Alla ``*_nasta_*``-flaggor nollställs automatiskt när de har använts.
    """

    svara_inte_pa_init: bool = False
    fel_synkbyte: bool = False
    tappa_nasta_kvittens: bool = False
    korrupt_nasta_kvittens: bool = False
    tyst_pa_nasta_kommando: bool = False
    korrupt_etx_i_nasta_svar: bool = False
    fel_raknare_i_nasta_svar: bool = False


class _ECUKanal(KWPKanal):
    """Blockkanal med möjlighet att injicera protokollfel."""

    def __init__(self, transport: Minnestransport, fel: Felinjektion, **kw: object) -> None:
        super().__init__(transport, **kw)  # type: ignore[arg-type]
        self.fel = fel

    def _kvittera(self, b: int) -> None:
        if self.fel.tappa_nasta_kvittens:
            self.fel.tappa_nasta_kvittens = False
            return  # tyst – klienten ska få timeout
        if self.fel.korrupt_nasta_kvittens:
            self.fel.korrupt_nasta_kvittens = False
            if self.ack_fordrojning:
                time.sleep(self.ack_fordrojning)
            self.skriv_ra(b)  # fel komplement
            return
        super()._kvittera(b)

    def skicka_block(self, titel: int, data: bytes | list[int] = b"") -> Block:
        if self.fel.korrupt_etx_i_nasta_svar:
            self.fel.korrupt_etx_i_nasta_svar = False
            return self._skicka_block_ra(titel, bytes(data), etx=0x00)
        if self.fel.fel_raknare_i_nasta_svar:
            self.fel.fel_raknare_i_nasta_svar = False
            return self._skicka_block_ra(titel, bytes(data), raknaroffset=7)
        return super().skicka_block(titel, data)

    def _skicka_block_ra(
        self, titel: int, data: bytes, etx: int = ETX, raknaroffset: int = 0
    ) -> Block:
        """Skicka ett block med avsiktligt fel i räknare eller avslutningsbyte."""
        langd = 3 + len(data)
        raknare = (self.nasta_raknare() + raknaroffset) & 0xFF
        ram = bytes([langd, raknare, titel & 0xFF]) + data
        for b in ram:
            self.skriv_ra(b)
            self.las_ra()  # klientens kvittens, ignoreras medvetet
        self.skriv_ra(etx)
        return Block(titel & 0xFF, data, raknare)


class SimuleradECU(threading.Thread):
    """En virtuell ECU som svarar på KWP1281 över en :class:`Minnestransport`."""

    def __init__(
        self,
        transport: Minnestransport,
        adress: int = 0x01,
        fel: Felinjektion | None = None,
        delnummer: str = "1Z9906019 ",
        komponent: str = "TDI SIMULATOR   ",
        kodning: int = 1,
        wsc: int = 12345,
        sessionstimeout: float = 5.0,
        bytetimeout: float = 1.0,
        ack_fordrojning: float = 0.0,
        klocka: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(name="simulerad-ecu", daemon=True)
        self.transport = transport
        self.adress = adress
        self.fel = fel or Felinjektion()
        self.delnummer = delnummer
        self.komponent = komponent
        self.kodning = kodning
        self.wsc = wsc
        self.sessionstimeout = sessionstimeout
        self.bytetimeout = bytetimeout
        self.ack_fordrojning = ack_fordrojning
        self.klocka = klocka or time.monotonic
        self._t0 = self.klocka()

        self._stopp = threading.Event()
        self.felkoder: list[tuple[int, int]] = list(SIM_FELKODER)
        self.anpassning: dict[int, int] = dict(SIM_ANPASSNING)
        self.inloggad = False
        self.sessioner = 0
        self.raderingar = 0
        self._stalldonsindex = 0
        self.senaste_fel: str = ""

    # -- livscykel ---------------------------------------------------------

    def stoppa(self) -> None:
        """Be simulatortråden avsluta och vänta in den."""
        self._stopp.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3.0)

    def run(self) -> None:  # pragma: no cover - trådstart täcks indirekt
        while not self._stopp.is_set():
            try:
                adress = self.transport.las_5baud_adress(0.1)
            except KWPTimeout:
                continue
            except VagdiagFel:
                return
            if adress != self.adress:
                continue  # tyst styrdon på den adressen
            try:
                self._session()
            except VagdiagFel as f:
                self.senaste_fel = str(f)
            except Exception as f:  # skyddar tråden mot oväntade fel
                self.senaste_fel = f"internt simulatorfel: {f!r}"

    # -- session -----------------------------------------------------------

    def _session(self) -> None:
        """Kör en komplett diagnossession från väckning till avslut."""
        if self.fel.svara_inte_pa_init:
            self.fel.svara_inte_pa_init = False
            return

        kanal = _ECUKanal(
            self.transport,
            self.fel,
            timeout=self.bytetimeout,
            ack_fordrojning=self.ack_fordrojning,
        )
        self.sessioner += 1
        self._stalldonsindex = 0
        self.inloggad = False

        time.sleep(0.01)
        kanal.skriv_ra(0x00 if self.fel.fel_synkbyte else 0x55)
        if self.fel.fel_synkbyte:
            self.fel.fel_synkbyte = False
            return
        kanal.skriv_ra(KEYBYTE_1)
        kanal.skriv_ra(KEYBYTE_2)

        komplement = kanal.las_ra(2.0)
        if komplement != (~KEYBYTE_2) & 0xFF:
            return  # klienten svarade fel – avbryt tyst, precis som ett styrdon

        for data in self._identblock():
            kanal.skicka_block(Blocktitel.IDENT, data)
            kanal.las_block()  # klientens ACK
        kanal.skicka_block(Blocktitel.ACK)

        # Vänta på kommandon i korta pass, så att tråden både kan avslutas
        # snabbt och tappa sessionen när bussen varit tyst för länge – precis
        # som ett riktigt styrdon gör utan keep-alive.
        senaste = time.monotonic()
        while not self._stopp.is_set():
            try:
                block = kanal.las_block(0.05)
            except KWPTimeout:
                if time.monotonic() - senaste > self.sessionstimeout:
                    return
                continue
            senaste = time.monotonic()
            if not self._hantera(kanal, block):
                return

    def _identblock(self) -> list[bytes]:
        """Identifikationsblockens nyttolaster."""
        kodningsblock = bytes(
            [
                (self.kodning >> 14) & 0x7F,
                (self.kodning >> 7) & 0x7F,
                self.kodning & 0x7F,
                (self.wsc >> 7) & 0x7F,
                self.wsc & 0x7F,
            ]
        )
        return [
            self.delnummer.encode("latin-1"),
            self.komponent.encode("latin-1"),
            kodningsblock,
        ]

    # -- kommandohantering -------------------------------------------------

    def _hantera(self, kanal: _ECUKanal, block: Block) -> bool:
        """Besvara ett kommandoblock. Returnerar False när sessionen ska avslutas."""
        if self.fel.tyst_pa_nasta_kommando:
            self.fel.tyst_pa_nasta_kommando = False
            return True  # inget svar – klienten får timeout

        titel = block.titel
        data = block.data

        if titel == Blocktitel.AVSLUTA:
            return False

        if titel == Blocktitel.ACK:
            kanal.skicka_block(Blocktitel.ACK)
            return True

        if titel == Blocktitel.LAS_FELKODER:
            self._skicka_felkoder(kanal)
            return True

        if titel == Blocktitel.RADERA_FELKODER:
            self.felkoder.clear()
            self.raderingar += 1
            kanal.skicka_block(Blocktitel.ACK)
            return True

        if titel in (Blocktitel.LAS_MATGRUPP, Blocktitel.GRUNDINSTALLNING):
            grupp = data[0] if data else 1
            varden = self.matgrupp(grupp)
            if not varden:
                kanal.skicka_block(Blocktitel.ACK)
            else:
                kanal.skicka_block(Blocktitel.MATVARDEN, varden)
            return True

        if titel == Blocktitel.STALLDONSTEST:
            if self._stalldonsindex < len(SIM_STALLDON):
                kod = SIM_STALLDON[self._stalldonsindex]
                self._stalldonsindex += 1
                kanal.skicka_block(
                    Blocktitel.STALLDONSSVAR, bytes([(kod >> 8) & 0xFF, kod & 0xFF])
                )
            else:
                kanal.skicka_block(Blocktitel.ACK)
            return True

        if titel == Blocktitel.LAS_ANPASSNING:
            kanal_nr = data[0] if data else 0
            self._svara_anpassning(kanal, kanal_nr, self.anpassning.get(kanal_nr, 0))
            return True

        if titel == Blocktitel.TESTA_ANPASSNING:
            kanal_nr = data[0] if data else 0
            varde = (data[1] << 8) | data[2] if len(data) >= 3 else 0
            self._svara_anpassning(kanal, kanal_nr, varde)
            return True

        if titel == Blocktitel.SPARA_ANPASSNING:
            kanal_nr = data[0] if data else 0
            varde = (data[1] << 8) | data[2] if len(data) >= 3 else 0
            self.anpassning[kanal_nr] = varde
            self._svara_anpassning(kanal, kanal_nr, varde)
            return True

        if titel == Blocktitel.LOGIN:
            kod = (data[0] << 8) | data[1] if len(data) >= 2 else 0
            self.inloggad = kod == SIM_LOGIN
            kanal.skicka_block(
                Blocktitel.ACK if self.inloggad else Blocktitel.EJ_TILLGANGLIG
            )
            return True

        # Okänt kommando – styrdonet svarar "ej tillgänglig".
        kanal.skicka_block(Blocktitel.EJ_TILLGANGLIG)
        return True

    def _skicka_felkoder(self, kanal: _ECUKanal) -> None:
        """Skicka felkodsminnet, max fyra koder per block."""
        if not self.felkoder:
            kanal.skicka_block(Blocktitel.FELKODER, bytes([0xFF, 0xFF, 0x88]))
            kanal.las_block()  # klientens ACK
            kanal.skicka_block(Blocktitel.ACK)
            return
        rader = list(self.felkoder)
        for start in range(0, len(rader), 4):
            bit = rader[start:start + 4]
            data = bytearray()
            for kod, status in bit:
                data += bytes([(kod >> 8) & 0xFF, kod & 0xFF, status & 0xFF])
            kanal.skicka_block(Blocktitel.FELKODER, bytes(data))
            kanal.las_block()  # klientens ACK
        kanal.skicka_block(Blocktitel.ACK)

    def _svara_anpassning(self, kanal: _ECUKanal, kanal_nr: int, varde: int) -> None:
        """Skicka ett 0xE6-svar med kanal, värde och tre mätvärden."""
        data = bytearray([kanal_nr & 0xFF, (varde >> 8) & 0xFF, varde & 0xFF])
        for trippel in (
            formler.koda(1, self._varvtal(), a=100),
            formler.koda(5, 85.0, a=10),
            formler.koda(6, 13.8, a=100),
        ):
            data += bytes(trippel)
        kanal.skicka_block(Blocktitel.ANPASSNINGSSVAR, bytes(data))

    # -- simulerade mätvärden ---------------------------------------------

    def _t(self) -> float:
        """Sekunder sedan simulatorn startade."""
        return self.klocka() - self._t0

    def _varvtal(self) -> float:
        """Varvtal som pendlar mellan tomgång och ca 3300 1/min."""
        fas = math.sin(self._t() / 7.0)
        return 850.0 + 1250.0 * (fas + 1.0)

    def _gaspadrag(self) -> float:
        """Normaliserat gaspådrag 0–1 ur varvtalsprofilen."""
        return max(0.0, min(1.0, (self._varvtal() - 850.0) / 2500.0))

    def matgrupp(self, grupp: int) -> bytes:
        """Bygg nyttolasten för ett mätvärdesblock (fyra värden om tre bytes)."""
        rpm = self._varvtal()
        gas = self._gaspadrag()
        t = self._t()

        if grupp == 1:
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(51, 8.0 + 42.0 * gas, a=40),
                formler.koda(5, 84.0 + 2.0 * math.sin(t / 11.0), a=10),
                formler.koda(2, 100.0 * gas, a=200),
            ]
        elif grupp == 2:
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(51, 8.0 + 42.0 * gas, a=40),
                formler.koda(2, 100.0 * gas, a=200),
                (16, 0xFF, 0b00000011 if gas < 0.05 else 0b00000001),
            ]
        elif grupp == 3:
            # Luftmassa BÖR/ÄR i mg/slag plus EGR-styrgrad. Den simulerade bilen
            # ligger lite lågt på ÄR-värdet vid full gas, precis som en sotig EGR.
            bor = 320.0 + 560.0 * gas
            ar = bor * (0.97 - 0.16 * gas)
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(51, bor, a=40),
                formler.koda(51, ar, a=40),
                formler.koda(2, max(0.0, 62.0 - 60.0 * gas), a=200),
            ]
        elif grupp == 4:
            bor = 2.0 + 9.5 * gas
            ar = bor - 0.4 * gas
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(4, bor, a=20),
                formler.koda(4, ar, a=20),
                formler.koda(2, 30.0 + 40.0 * gas, a=200),
            ]
        elif grupp == 11:
            bor = 1000.0 + 1050.0 * gas
            ar = bor - 90.0 * gas * gas
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(18, bor, a=200),
                formler.koda(18, ar, a=200),
                formler.koda(2, 20.0 + 65.0 * gas, a=200),
            ]
        elif grupp == 13:
            avvikelser = [
                1.4 * math.sin(t / 3.0),
                -0.6 + 0.3 * math.sin(t / 5.0),
                0.2 * math.sin(t / 4.0),
                -0.9 + 0.4 * math.sin(t / 6.0),
            ]
            # a=2 ger 0,2 mg/slag per steg – tillräckligt fint för att
            # se en cylinder som avviker mer än ±2 mg/slag.
            varden = [formler.koda(39, v, a=2) for v in avvikelser]
        elif grupp in (5, 6, 7, 8, 9, 10, 12):
            varden = [
                formler.koda(1, rpm, a=100),
                formler.koda(51, 8.0 + 42.0 * gas, a=40),
                formler.koda(5, 84.0, a=10),
                formler.koda(6, 13.9 - 0.4 * gas, a=100),
            ]
        else:
            return b""  # gruppen finns inte i det här styrdonet

        data = bytearray()
        for trippel in varden:
            data += bytes(trippel)
        return bytes(data)


# ---------------------------------------------------------------------------
# Bekvämlighetsfunktion
# ---------------------------------------------------------------------------


@dataclass
class Simulatorkoppling:
    """En startad simulator plus klienttransporten som pratar med den."""

    transport: Minnestransport
    ecu: SimuleradECU

    def stang(self) -> None:
        """Stoppa simulatorn och stäng transporten."""
        self.ecu.stoppa()
        self.transport.stang()

    def __enter__(self) -> Simulatorkoppling:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stang()


def starta_simulator(adress: int = 0x01, **kw: object) -> Simulatorkoppling:
    """Starta en virtuell ECU och returnera kopplingen till den."""
    klient, ecu_transport = skapa_lankat_par()
    ecu = SimuleradECU(ecu_transport, adress=adress, **kw)  # type: ignore[arg-type]
    ecu.start()
    return Simulatorkoppling(transport=klient, ecu=ecu)
