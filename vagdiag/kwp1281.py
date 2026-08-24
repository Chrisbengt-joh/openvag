"""KWP1281 över K-line – ren protokollimplementation utan användargränssnitt.

Protokollet i korthet
---------------------

1. **5-baud-init**: styrdonsadressen skickas som break-pulser, 200 ms per bit.
   Styrdonet svarar ``0x55``, nyckelbyte 1, nyckelbyte 2. Efter ~30 ms svarar
   vi med komplementet av nyckelbyte 2.
2. **Blockformat**: ``[längd][blockräknare][titel][data...][0x03]`` där
   ``längd = 3 + antal databytes``.
3. **Bytekvittens**: varje byte utom det avslutande ``0x03`` kvitteras av
   mottagaren med sitt komplement. Vi lägger in en liten fördröjning före
   vår kvittens så att långsamma styrdon hinner med.
4. **Blockräknaren** delas av båda parter och ökar med 1 per block (0xFF→0x00).
5. **Keep-alive**: går det mer än ~1 s utan trafik tappar styrdonet sessionen,
   så ett ACK-block (0x09) måste skickas regelbundet.

Val av keep-alive-lösning
-------------------------
Keep-alive körs i en **bakgrundstråd** (:meth:`KWP1281.starta_keepalive`).
Skälet är att terminal- och GUI-lägena båda har lägen där huvudtråden blockerar
på tangentbordsinmatning (menyval, bekräftelseord) – då hade en pump i
huvudloopen tappat sessionen. All blocktrafik skyddas av ett ``RLock`` så att
tråden aldrig kan hamna mitt i ett annat kommando. Behöver man ett strikt
enkeltrådat läge finns :meth:`KWP1281.keep_alive` att anropa manuellt.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterable, Sequence

from .felkoder import Felkod, avkoda_block, stalldonsnamn
from .formler import Matvarde, berakna
from .styrdon import AUTOSCAN_ADRESSER, styrdonsnamn
from .transport import Transport
from .undantag import (
    KWPAnslutningsFel,
    KWPFel,
    KWPNekat,
    KWPProtokollFel,
    KWPTimeout,
    VagdiagFel,
)

__all__ = [
    "Blocktitel",
    "Block",
    "KWPKanal",
    "KWP1281",
    "Identifikation",
    "Anpassning",
    "Stalldon",
    "Skanresultat",
    "skanna",
    "ETX",
]

#: Avslutande byte i varje block.
ETX = 0x03

#: Synkbyte som styrdonet skickar efter lyckad 5-baud-väckning.
SYNK = 0x55


class Blocktitel(IntEnum):
    """Blocktitlar (kommandon och svar) i KWP1281."""

    # Kommandon vi skickar
    STALLDONSTEST = 0x04
    RADERA_FELKODER = 0x05
    AVSLUTA = 0x06
    LAS_FELKODER = 0x07
    ACK = 0x09
    OMKODNING = 0x10          # implementeras EJ i v1 – för riskabelt
    LAS_ANPASSNING = 0x21
    TESTA_ANPASSNING = 0x22
    GRUNDINSTALLNING = 0x28
    LAS_MATGRUPP = 0x29
    SPARA_ANPASSNING = 0x2A
    LOGIN = 0x2B

    # Svar från styrdonet
    ANPASSNINGSSVAR = 0xE6
    MATVARDEN = 0xE7
    EJ_TILLGANGLIG = 0xF4
    STALLDONSSVAR = 0xF5
    IDENT = 0xF6
    KODNING = 0xF7
    FELKODER = 0xFC


def _namn_pa_titel(titel: int) -> str:
    """Läsbart namn på en blocktitel (för fel- och felsökningsmeddelanden)."""
    try:
        return Blocktitel(titel).name
    except ValueError:
        return f"0x{titel:02X}"


@dataclass(frozen=True)
class Block:
    """Ett mottaget eller skickat KWP1281-block."""

    titel: int
    data: bytes = b""
    raknare: int = 0

    @property
    def titelnamn(self) -> str:
        """Blocktitelns namn i klartext."""
        return _namn_pa_titel(self.titel)

    def __str__(self) -> str:  # pragma: no cover - felsökningshjälp
        hexdata = " ".join(f"{b:02X}" for b in self.data)
        return f"<Block {self.titelnamn} rakn={self.raknare} data=[{hexdata}]>"


# ---------------------------------------------------------------------------
# Gemensamt byte- och blocklager
# ---------------------------------------------------------------------------


class KWPKanal:
    """Byte- och blocklager för KWP1281.

    Används av både klienten (:class:`KWP1281`) och ECU-simulatorn, så att
    testerna kör exakt samma kvittens- och ekologik som riktig hårdvara.
    """

    def __init__(
        self,
        transport: Transport,
        timeout: float = 1.0,
        ack_fordrojning: float = 0.0015,
        kontrollera_eko: bool = True,
        kontrollera_raknare: bool = True,
        spar: Callable[[str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.ack_fordrojning = ack_fordrojning
        self.kontrollera_eko = kontrollera_eko
        self.kontrollera_raknare = kontrollera_raknare
        self.raknare = 0
        self.senaste_trafik = time.monotonic()
        self._raknare_synkad = False
        self._spar = spar

    # -- bytenivå ----------------------------------------------------------

    def _skriv_byte(self, b: int) -> None:
        """Skriv en byte och konsumera ekot från K-line."""
        b &= 0xFF
        self.transport.skriv_byte(b)
        self.senaste_trafik = time.monotonic()
        if not self.transport.ekar:
            return
        eko = self.transport.las_byte(self.timeout)
        if self.kontrollera_eko and eko != b:
            raise KWPProtokollFel(
                f"Ekofel: skickade 0x{b:02X} men fick tillbaka 0x{eko:02X}. "
                "K-line-kabeln eller latensinställningen är troligen problemet."
            )

    def _las_byte(self, timeout: float | None = None) -> int:
        """Läs en byte från bussen."""
        b = self.transport.las_byte(self.timeout if timeout is None else timeout)
        self.senaste_trafik = time.monotonic()
        return b

    def _kvittera(self, b: int) -> None:
        """Svara med komplementet av mottagen byte."""
        if self.ack_fordrojning:
            time.sleep(self.ack_fordrojning)
        self._skriv_byte((~b) & 0xFF)

    def skriv_ra(self, b: int) -> None:
        """Skriv en byte utanför blockstrukturen (används i init-sekvensen)."""
        self._skriv_byte(b)

    def las_ra(self, timeout: float | None = None) -> int:
        """Läs en byte utanför blockstrukturen (används i init-sekvensen)."""
        return self._las_byte(timeout)

    # -- blocknivå ---------------------------------------------------------

    def nasta_raknare(self) -> int:
        """Stega blockräknaren (wrap 0xFF -> 0x00) och returnera det nya värdet."""
        self.raknare = (self.raknare + 1) & 0xFF
        return self.raknare

    def skicka_block(self, titel: int, data: bytes | Sequence[int] = b"") -> Block:
        """Skicka ett block och invänta mottagarens kvittens på varje byte."""
        nyttolast = bytes(data)
        langd = 3 + len(nyttolast)
        if langd > 0xFF:
            raise KWPProtokollFel("Blocket är för långt för KWP1281 (max 252 databytes).")
        raknare = self.nasta_raknare()
        ram = bytes([langd, raknare, titel & 0xFF]) + nyttolast
        for b in ram:
            self._skriv_byte(b)
            kvitto = self._las_byte()
            if kvitto != (~b) & 0xFF:
                raise KWPProtokollFel(
                    f"Felaktig kvittens på byte 0x{b:02X}: väntade "
                    f"0x{(~b) & 0xFF:02X}, fick 0x{kvitto:02X}."
                )
        self._skriv_byte(ETX)
        block = Block(titel & 0xFF, nyttolast, raknare)
        if self._spar:
            self._spar(f"-> {block}")
        return block

    def las_block(self, timeout: float | None = None) -> Block:
        """Läs ett block och kvittera varje byte utom det avslutande 0x03."""
        langd = self._las_byte(timeout)
        if langd < 3:
            raise KWPProtokollFel(
                f"Ogiltig blocklängd {langd} – förväntade minst 3. "
                "Sessionen är ur synk."
            )
        self._kvittera(langd)

        raknare = self._las_byte()
        self._kvittera(raknare)
        if self.kontrollera_raknare and self._raknare_synkad:
            vantat = (self.raknare + 1) & 0xFF
            if raknare != vantat:
                raise KWPProtokollFel(
                    f"Blockräknaren hoppade: väntade {vantat}, fick {raknare}. "
                    "Sessionen är ur synk."
                )

        titel = self._las_byte()
        self._kvittera(titel)

        data = bytearray()
        for _ in range(langd - 3):
            b = self._las_byte()
            self._kvittera(b)
            data.append(b)

        etx = self._las_byte()
        if etx != ETX:
            raise KWPProtokollFel(
                f"Blocket avslutades med 0x{etx:02X} i stället för 0x03."
            )

        self.raknare = raknare
        self._raknare_synkad = True
        block = Block(titel, bytes(data), raknare)
        if self._spar:
            self._spar(f"<- {block}")
        return block


# ---------------------------------------------------------------------------
# Datatyper för klienten
# ---------------------------------------------------------------------------


@dataclass
class Identifikation:
    """Identifikationsuppgifter som styrdonet skickar efter init."""

    adress: int
    kb1: int = 0
    kb2: int = 0
    delnummer: str = ""
    komponent: str = ""
    kodning: int | None = None
    wsc: int | None = None
    textrader: list[str] = field(default_factory=list)
    ra_block: list[bytes] = field(default_factory=list)

    @property
    def namn(self) -> str:
        """Styrdonets svenska namn."""
        return styrdonsnamn(self.adress)

    def sammanfattning(self) -> str:
        """En kompakt rad om styrdonet."""
        delar = [f"0x{self.adress:02X} {self.namn}"]
        if self.delnummer:
            delar.append(self.delnummer)
        if self.komponent:
            delar.append(self.komponent)
        if self.kodning is not None:
            delar.append(f"kodning {self.kodning}")
        if self.wsc is not None:
            delar.append(f"WSC {self.wsc}")
        return " | ".join(delar)


@dataclass(frozen=True)
class Anpassning:
    """Svar på anpassningskommando (0x21/0x22/0x2A)."""

    kanal: int
    varde: int
    matvarden: list[Matvarde] = field(default_factory=list)


@dataclass(frozen=True)
class Stalldon:
    """Ett ställdon i ställdonstestsekvensen."""

    kod: int
    ra: bytes

    @property
    def namn(self) -> str:
        """Ställdonets namn ur databasen."""
        return stalldonsnamn(self.kod)


@dataclass
class Skanresultat:
    """Resultatet av att prova ett styrdon under auto-scan."""

    adress: int
    svarade: bool = False
    ident: Identifikation | None = None
    felkoder: list[Felkod] = field(default_factory=list)
    fel: str = ""

    @property
    def namn(self) -> str:
        """Styrdonets svenska namn."""
        return styrdonsnamn(self.adress)


# ---------------------------------------------------------------------------
# Klienten
# ---------------------------------------------------------------------------


class KWP1281(KWPKanal):
    """Diagnosklient för ett KWP1281-styrdon."""

    #: Max antal identifikationsblock vi accepterar innan vi ger upp.
    MAX_IDENTBLOCK = 40

    def __init__(
        self,
        transport: Transport,
        timeout: float = 1.0,
        init_timeout: float = 2.0,
        ack_fordrojning: float = 0.0015,
        keepalive_intervall: float = 0.8,
        spar: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(transport, timeout=timeout, ack_fordrojning=ack_fordrojning,
                         spar=spar)
        self.init_timeout = init_timeout
        self.keepalive_intervall = keepalive_intervall
        self.ansluten = False
        self.adress: int | None = None
        self.ident: Identifikation | None = None
        self._las = threading.RLock()
        self._ka_trad: threading.Thread | None = None
        self._ka_stopp = threading.Event()
        self._ka_fel: KWPFel | None = None

    # -- anslutning --------------------------------------------------------

    def anslut(self, adress: int, forsok: int = 2) -> Identifikation:
        """Väck styrdonet på ``adress`` och läs in identifikationsblocken."""
        sista: KWPFel | None = None
        for nr in range(max(1, forsok)):
            try:
                return self._anslut_en_gang(adress)
            except KWPFel as fel:
                sista = fel
                self.ansluten = False
                if nr + 1 < forsok:
                    time.sleep(0.5)
        assert sista is not None
        raise sista

    def _anslut_en_gang(self, adress: int) -> Identifikation:
        with self._las:
            self.stoppa_keepalive()
            self.ansluten = False
            self.raknare = 0
            self._raknare_synkad = False
            self.transport.rensa_in()
            self.transport.skicka_5baud_adress(adress)

            try:
                synk = self._las_byte(self.init_timeout)
            except KWPTimeout as fel:
                raise KWPAnslutningsFel(
                    f"Inget svar från styrdon 0x{adress:02X} "
                    f"({styrdonsnamn(adress)}) efter 5-baud-väckning."
                ) from fel
            if synk != SYNK:
                raise KWPAnslutningsFel(
                    f"Förväntade synkbyte 0x55 från 0x{adress:02X}, fick 0x{synk:02X}."
                )

            kb1 = self._las_byte(self.init_timeout)
            kb2 = self._las_byte(self.init_timeout)

            time.sleep(0.03)
            self._skriv_byte((~kb2) & 0xFF)

            ident = Identifikation(adress=adress, kb1=kb1, kb2=kb2)
            self._las_identblock(ident)

            self.adress = adress
            self.ident = ident
            self.ansluten = True
            self._ka_fel = None
            return ident

    def _las_identblock(self, ident: Identifikation) -> None:
        """Läs identifikationsblocken (0xF6) tills styrdonet skickar ACK."""
        for _ in range(self.MAX_IDENTBLOCK):
            block = self.las_block(self.init_timeout)
            if block.titel == Blocktitel.ACK:
                return
            ident.ra_block.append(block.data)
            if block.titel in (Blocktitel.IDENT, Blocktitel.KODNING):
                self._tolka_identblock(ident, block.data)
            self.skicka_block(Blocktitel.ACK)
        raise KWPProtokollFel(
            "Styrdonet slutade aldrig skicka identifikationsblock."
        )

    @staticmethod
    def _tolka_identblock(ident: Identifikation, data: bytes) -> None:
        """Tolka ett identifikationsblock som text eller kodningsblock."""
        if not data:
            return
        skrivbart = all(32 <= b < 127 for b in data)
        if skrivbart:
            text = data.decode("latin-1").strip()
            if not text:
                return
            ident.textrader.append(text)
            if not ident.delnummer and _ser_ut_som_delnummer(text):
                ident.delnummer = text
            elif not ident.komponent:
                ident.komponent = text
            return
        # Kodningsblock: 7-bitars packning av kodning + verkstadskod (WSC).
        if len(data) >= 5:
            ident.kodning = ((data[0] & 0x7F) << 14) | ((data[1] & 0x7F) << 7) | (
                data[2] & 0x7F
            )
            ident.wsc = ((data[3] & 0x7F) << 7) | (data[4] & 0x7F)

    def koppla_ner(self) -> None:
        """Avsluta sessionen snyggt (block 0x06). Fel ignoreras."""
        self.stoppa_keepalive()
        with self._las:
            if self.ansluten:
                try:
                    self.skicka_block(Blocktitel.AVSLUTA)
                except VagdiagFel:
                    pass
            self.ansluten = False
            self.adress = None

    def __enter__(self) -> KWP1281:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.koppla_ner()

    # -- keep-alive --------------------------------------------------------

    def keep_alive(self) -> None:
        """Skicka ett ACK-block för att hålla sessionen vid liv."""
        self._kommando(Blocktitel.ACK, forvantat=(Blocktitel.ACK,))

    def starta_keepalive(self, intervall: float | None = None) -> None:
        """Starta bakgrundstråden som håller sessionen vid liv."""
        if intervall is not None:
            self.keepalive_intervall = intervall
        if self._ka_trad and self._ka_trad.is_alive():
            return
        self._ka_stopp.clear()
        self._ka_trad = threading.Thread(
            target=self._keepalive_loop, name="kwp1281-keepalive", daemon=True
        )
        self._ka_trad.start()

    def stoppa_keepalive(self) -> None:
        """Stoppa keep-alive-tråden och vänta in den."""
        trad = self._ka_trad
        self._ka_stopp.set()
        self._ka_trad = None
        if trad and trad.is_alive() and trad is not threading.current_thread():
            trad.join(timeout=2.0)

    def _keepalive_loop(self) -> None:
        """Skickar ACK-block när bussen varit tyst för länge."""
        while not self._ka_stopp.wait(0.05):
            if not self.ansluten:
                continue
            if time.monotonic() - self.senaste_trafik < self.keepalive_intervall:
                continue
            try:
                with self._las:
                    if not self.ansluten or self._ka_stopp.is_set():
                        continue
                    if time.monotonic() - self.senaste_trafik < self.keepalive_intervall:
                        continue
                    self.skicka_block(Blocktitel.ACK)
                    self.las_block()
            except VagdiagFel as fel:
                self.ansluten = False
                self._ka_fel = fel if isinstance(fel, KWPFel) else KWPFel(str(fel))
                return

    @property
    def bakgrundsfel(self) -> KWPFel | None:
        """Fel som keep-alive-tråden råkat ut för, om något."""
        return self._ka_fel

    # -- kommandon ---------------------------------------------------------

    def _krav_ansluten(self) -> None:
        if not self.ansluten:
            if self._ka_fel is not None:
                raise self._ka_fel
            raise KWPFel(
                "Ingen aktiv session – anslut till styrdonet först.",
                tips="Slå av tändningen i 5 sekunder, slå på den igen och anslut om.",
            )

    def _kommando(
        self,
        titel: int,
        data: bytes | Sequence[int] = b"",
        forvantat: Iterable[int] | None = None,
        timeout: float | None = None,
    ) -> Block:
        """Skicka ett kommandoblock och läs svaret."""
        with self._las:
            self._krav_ansluten()
            try:
                self.skicka_block(titel, data)
                svar = self.las_block(timeout)
            except KWPFel:
                self.ansluten = False
                raise
            if forvantat is not None and svar.titel not in tuple(forvantat):
                vantat = ", ".join(_namn_pa_titel(t) for t in forvantat)
                raise KWPNekat(
                    f"Styrdonet svarade {svar.titelnamn} på "
                    f"{_namn_pa_titel(titel)} (väntade {vantat})."
                )
            return svar

    # -- felkoder ----------------------------------------------------------

    def las_felkoder(self) -> list[Felkod]:
        """Läs alla felkoder. Flera 0xFC-block hanteras och kvitteras."""
        with self._las:
            self._krav_ansluten()
            koder: list[Felkod] = []
            try:
                self.skicka_block(Blocktitel.LAS_FELKODER)
                for _ in range(64):
                    block = self.las_block()
                    if block.titel == Blocktitel.ACK:
                        break
                    if block.titel != Blocktitel.FELKODER:
                        raise KWPNekat(
                            f"Oväntat svar {block.titelnamn} vid felkodsläsning."
                        )
                    koder.extend(avkoda_block(block.data))
                    self.skicka_block(Blocktitel.ACK)
                else:
                    raise KWPProtokollFel("Styrdonet slutade aldrig skicka felkoder.")
            except KWPFel:
                self.ansluten = False
                raise
            return koder

    def radera_felkoder(self) -> None:
        """Radera felkodsminnet (block 0x05)."""
        self._kommando(Blocktitel.RADERA_FELKODER, forvantat=(Blocktitel.ACK,))

    # -- mätvärden ---------------------------------------------------------

    @staticmethod
    def _tolka_matvarden(data: bytes) -> list[Matvarde]:
        """Dela upp en nyttolast i grupper om tre bytes och räkna om dem."""
        varden: list[Matvarde] = []
        for i in range(0, len(data) - 2, 3):
            varden.append(berakna(data[i], data[i + 1], data[i + 2]))
        return varden

    def las_matgrupp(self, grupp: int) -> list[Matvarde]:
        """Läs ett mätvärdesblock (block 0x29). Tom lista om gruppen saknas."""
        svar = self._kommando(
            Blocktitel.LAS_MATGRUPP,
            bytes([grupp & 0xFF]),
            forvantat=(Blocktitel.MATVARDEN, Blocktitel.ACK, Blocktitel.EJ_TILLGANGLIG),
        )
        if svar.titel != Blocktitel.MATVARDEN:
            return []
        return self._tolka_matvarden(svar.data)

    def grundinstallning(self, grupp: int) -> list[Matvarde]:
        """Starta/läs grundinställning för en grupp (block 0x28).

        VARNING: grundinställning kan starta ställdon och ändra adaptioner.
        """
        svar = self._kommando(
            Blocktitel.GRUNDINSTALLNING,
            bytes([grupp & 0xFF]),
            forvantat=(
                Blocktitel.MATVARDEN,
                Blocktitel.ANPASSNINGSSVAR,
                Blocktitel.ACK,
                Blocktitel.EJ_TILLGANGLIG,
            ),
        )
        if svar.titel == Blocktitel.MATVARDEN:
            return self._tolka_matvarden(svar.data)
        if svar.titel == Blocktitel.ANPASSNINGSSVAR and len(svar.data) > 3:
            return self._tolka_matvarden(svar.data[3:])
        return []

    # -- ställdonstest -----------------------------------------------------

    def stalldonstest_nasta(self) -> Stalldon | None:
        """Stega till nästa ställdon (block 0x04). None när sekvensen är slut."""
        svar = self._kommando(
            Blocktitel.STALLDONSTEST,
            forvantat=(Blocktitel.STALLDONSSVAR, Blocktitel.ACK,
                       Blocktitel.EJ_TILLGANGLIG),
            timeout=max(self.timeout, 2.0),
        )
        if svar.titel != Blocktitel.STALLDONSSVAR or len(svar.data) < 2:
            return None
        kod = (svar.data[0] << 8) | svar.data[1]
        if kod == 0:
            return None
        return Stalldon(kod=kod, ra=svar.data)

    # -- anpassning --------------------------------------------------------

    @staticmethod
    def _tolka_anpassning(svar: Block, kanal: int) -> Anpassning:
        """Tolka ett 0xE6-svar till en :class:`Anpassning`."""
        if len(svar.data) < 3:
            raise KWPProtokollFel(
                f"För kort anpassningssvar ({len(svar.data)} bytes)."
            )
        return Anpassning(
            kanal=svar.data[0] if svar.data[0] else kanal,
            varde=(svar.data[1] << 8) | svar.data[2],
            matvarden=KWP1281._tolka_matvarden(svar.data[3:]),
        )

    def las_anpassning(self, kanal: int) -> Anpassning:
        """Läs en anpassningskanal (block 0x21)."""
        svar = self._kommando(
            Blocktitel.LAS_ANPASSNING,
            bytes([kanal & 0xFF]),
            forvantat=(Blocktitel.ANPASSNINGSSVAR,),
        )
        return self._tolka_anpassning(svar, kanal)

    def testa_anpassning(self, kanal: int, varde: int) -> Anpassning:
        """Testa ett anpassningsvärde utan att spara (block 0x22)."""
        _krav_16bit(varde)
        svar = self._kommando(
            Blocktitel.TESTA_ANPASSNING,
            bytes([kanal & 0xFF, (varde >> 8) & 0xFF, varde & 0xFF]),
            forvantat=(Blocktitel.ANPASSNINGSSVAR,),
        )
        return self._tolka_anpassning(svar, kanal)

    def spara_anpassning(self, kanal: int, varde: int) -> Anpassning:
        """Spara ett anpassningsvärde permanent (block 0x2A).

        VARNING: skriver till styrdonets EEPROM. På kluster (0x17) och
        startspärr (0x25) kan felaktiga värden ge startspärr.
        """
        _krav_16bit(varde)
        svar = self._kommando(
            Blocktitel.SPARA_ANPASSNING,
            bytes([kanal & 0xFF, (varde >> 8) & 0xFF, varde & 0xFF]),
            forvantat=(Blocktitel.ANPASSNINGSSVAR, Blocktitel.ACK),
        )
        if svar.titel == Blocktitel.ACK:
            return Anpassning(kanal=kanal, varde=varde)
        return self._tolka_anpassning(svar, kanal)

    # -- login -------------------------------------------------------------

    def login(self, kod: int) -> bool:
        """Logga in med femsiffrig kod (block 0x2B). True vid ACK."""
        _krav_16bit(kod)
        svar = self._kommando(
            Blocktitel.LOGIN,
            bytes([(kod >> 8) & 0xFF, kod & 0xFF, 0x00]),
            forvantat=(Blocktitel.ACK, Blocktitel.EJ_TILLGANGLIG),
        )
        return svar.titel == Blocktitel.ACK


def _krav_16bit(varde: int) -> None:
    """Kontrollera att ett värde ryms i 0–65535."""
    if not 0 <= varde <= 0xFFFF:
        raise VagdiagFel(
            f"Värdet {varde} ligger utanför tillåtet intervall 0–65535.",
            tips="Ange ett femsiffrigt tal mellan 0 och 65535.",
        )


def _ser_ut_som_delnummer(text: str) -> bool:
    """Grov heuristik för VAG-delnummer, t.ex. ``028906021AB``."""
    kompakt = text.replace(" ", "")
    return (
        8 <= len(kompakt) <= 14
        and kompakt[0].isdigit()
        and sum(c.isdigit() for c in kompakt) >= 6
        and kompakt.isalnum()
    )


# ---------------------------------------------------------------------------
# Auto-scan
# ---------------------------------------------------------------------------


def skanna(
    transport: Transport,
    adresser: Iterable[int] = AUTOSCAN_ADRESSER,
    forsok: int = 2,
    timeout: float = 0.6,
    init_timeout: float = 1.2,
    paus: float = 0.6,
    aterkoppling: Callable[[Skanresultat], None] | None = None,
    spar: Callable[[str], None] | None = None,
) -> list[Skanresultat]:
    """Prova varje styrdonsadress: anslut, hämta ident, räkna felkoder, koppla ner.

    Adresser som inte svarar rapporteras som tysta – det är helt normalt att de
    flesta av dem saknas i en bil från 1999.
    """
    resultat: list[Skanresultat] = []
    for adress in adresser:
        post = Skanresultat(adress=adress)
        klient = KWP1281(
            transport,
            timeout=timeout,
            init_timeout=init_timeout,
            spar=spar,
        )
        try:
            post.ident = klient.anslut(adress, forsok=forsok)
            post.svarade = True
            try:
                post.felkoder = klient.las_felkoder()
            except VagdiagFel as fel:
                post.fel = f"Felkoder kunde inte läsas: {fel}"
        except VagdiagFel as fel:
            post.fel = str(fel)
        finally:
            try:
                klient.koppla_ner()
            except VagdiagFel:
                pass
            transport.rensa_in()
        resultat.append(post)
        if aterkoppling:
            aterkoppling(post)
        if paus:
            time.sleep(paus)
    return resultat
