"""Transportlager – abstraktion över K-line.

Protokollkoden i :mod:`vagdiag.kwp1281` pratar bara mot :class:`Transport`.
Det gör att exakt samma protokollstack kan köras mot

* en riktig KKL-kabel (:class:`Serietransport`), och
* en virtuell ECU i samma process (:class:`Minnestransport`),

utan com0com, pty:er eller andra OS-beroenden. Testerna använder den senare.

K-line är en halvduplex enledarbuss: **allt man skickar ekas tillbaka på den
egna mottagaren**. Minnestransporten emulerar detta exakt (varje skriven byte
hamnar både i den egna och i motpartens mottagningskö), så att echo-hanteringen
i protokollagret testas på riktigt i stället för att specialfallas bort.
"""

from __future__ import annotations

import os
import queue
import sys
import time
from abc import ABC, abstractmethod
from typing import Iterator

from .undantag import KWPTimeout, TransportFel

__all__ = [
    "Transport",
    "Serietransport",
    "Minnestransport",
    "skapa_lankat_par",
    "lista_portar",
    "pyserial_finns",
    "las_ftdi_latens",
    "satt_ftdi_latens",
]

#: Standardbaudrate för KWP1281 över K-line.
BAUD = 10400

#: Bittid för 5-baud-initiering (1/5 sekund per bit).
BIT_TID = 0.2


class Transport(ABC):
    """Gemensamt gränssnitt för allt som kan bära KWP1281-bytes."""

    #: Sant om transporten ekar tillbaka det man skriver (äkta K-line gör det).
    ekar: bool = True

    #: Beskrivande namn, används i loggar och felmeddelanden.
    namn: str = "okänd"

    @abstractmethod
    def skriv_byte(self, b: int) -> None:
        """Skriv en byte på bussen."""

    @abstractmethod
    def las_byte(self, timeout: float) -> int:
        """Läs en byte. Kastar :class:`KWPTimeout` om inget kommer i tid."""

    @abstractmethod
    def rensa_in(self) -> None:
        """Kasta allt som ligger och skräpar i mottagningsbufferten."""

    @abstractmethod
    def skicka_5baud_adress(self, adress: int) -> None:
        """Väck styrdonet med dess adress skickad i 5 baud."""

    @abstractmethod
    def stang(self) -> None:
        """Stäng transporten."""

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stang()


# ---------------------------------------------------------------------------
# Riktig serieport (FTDI FT232RL / KKL VAG 409.1)
# ---------------------------------------------------------------------------


class Serietransport(Transport):
    """K-line över en virtuell COM-port (KKL-kabel med FT232RL).

    ``pyserial`` importeras lazy så att resten av paketet – och hela
    testsviten – fungerar utan att pyserial är installerat.
    """

    ekar = True

    def __init__(
        self,
        port: str,
        baud: int = BAUD,
        timeout: float = 1.0,
        bit_tid: float = BIT_TID,
    ) -> None:
        try:
            import serial
        except ImportError as fel:  # pragma: no cover - beroendekontroll
            raise TransportFel(
                "pyserial är inte installerat. Kör: pip install pyserial",
                tips="pip install pyserial   (eller: pip install -e .)",
            ) from fel

        self.namn = port
        self.bit_tid = bit_tid
        try:
            self._ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=2.0,
            )
        except Exception as fel:  # pragma: no cover - kräver hårdvara
            raise TransportFel(f"Kunde inte öppna {port}: {fel}") from fel

        self._timeout = timeout
        #: FTDI Latency Timer i ms (None om okänt) – rådgivande, vi vägrar inte köra.
        self.latens_ms = las_ftdi_latens(port)

    # -- byteström ---------------------------------------------------------

    def skriv_byte(self, b: int) -> None:
        try:
            self._ser.write(bytes([b & 0xFF]))
            self._ser.flush()
        except Exception as fel:  # pragma: no cover - kräver hårdvara
            raise TransportFel(f"Skrivfel på {self.namn}: {fel}") from fel

    def las_byte(self, timeout: float) -> int:
        if timeout != self._timeout:
            self._ser.timeout = timeout
            self._timeout = timeout
        try:
            data = self._ser.read(1)
        except Exception as fel:  # pragma: no cover - kräver hårdvara
            raise TransportFel(f"Läsfel på {self.namn}: {fel}") from fel
        if not data:
            raise KWPTimeout(f"Inget svar från styrdonet inom {timeout:.2f} s.")
        return data[0]

    def rensa_in(self) -> None:
        try:
            self._ser.reset_input_buffer()
        except Exception:  # pragma: no cover - kräver hårdvara
            pass

    # -- 5-baud-väckning ---------------------------------------------------

    def skicka_5baud_adress(self, adress: int) -> None:
        """Bit-banga styrdonsadressen med break-condition, 200 ms per bit.

        Ordning: startbit (break PÅ), 8 databitar LSB först (0 = break PÅ,
        1 = break AV), stoppbit (break AV).
        """
        self.rensa_in()
        ser = self._ser
        try:
            ser.break_condition = True          # startbit
            time.sleep(self.bit_tid)
            for i in range(8):
                bit = (adress >> i) & 1
                ser.break_condition = bit == 0
                time.sleep(self.bit_tid)
            ser.break_condition = False         # stoppbit
            time.sleep(self.bit_tid)
        except Exception as fel:  # pragma: no cover - kräver hårdvara
            raise TransportFel(
                f"Kunde inte skicka 5-baud-adress på {self.namn}: {fel}"
            ) from fel
        finally:
            try:
                ser.break_condition = False
            except Exception:  # pragma: no cover
                pass
        self.rensa_in()

    def stang(self) -> None:
        try:
            self._ser.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Virtuell buss i minnet (för simulatorn och testerna)
# ---------------------------------------------------------------------------


class Minnestransport(Transport):
    """En ände av en virtuell K-line-buss.

    Skrivna bytes hamnar i *både* den egna kön (ekot) och motpartens kö,
    precis som på en riktig enledarbuss.
    """

    ekar = True

    def __init__(
        self,
        egen: queue.Queue[int],
        motpart: queue.Queue[int],
        fem_baud: queue.Queue[int],
        ar_klient: bool,
        namn: str = "minne",
    ) -> None:
        self._egen = egen
        self._motpart = motpart
        self._fem_baud = fem_baud
        self._ar_klient = ar_klient
        self.namn = namn
        self.stangd = False

    def skriv_byte(self, b: int) -> None:
        if self.stangd:
            raise TransportFel("Transporten är stängd.")
        b &= 0xFF
        self._egen.put(b)      # eko på egen mottagare
        self._motpart.put(b)   # motparten hör samma byte

    def las_byte(self, timeout: float) -> int:
        try:
            return self._egen.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise KWPTimeout(
                f"Inget svar på den virtuella bussen inom {timeout:.2f} s."
            ) from None

    def rensa_in(self) -> None:
        _tom(self._egen)

    def skicka_5baud_adress(self, adress: int) -> None:
        """Logisk motsvarighet till 5-baud-väckningen.

        Bussen är tyst medan väckningen pågår, så båda köerna töms först.
        """
        if not self._ar_klient:
            raise TransportFel("Endast klientsidan kan skicka 5-baud-adress.")
        _tom(self._egen)
        _tom(self._motpart)
        _tom(self._fem_baud)
        self._fem_baud.put(adress & 0xFF)

    def las_5baud_adress(self, timeout: float) -> int:
        """ECU-sidan: vänta på en 5-baud-väckning. Kastar KWPTimeout."""
        if self._ar_klient:
            raise TransportFel("Endast ECU-sidan kan läsa 5-baud-adress.")
        try:
            return self._fem_baud.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise KWPTimeout("Ingen 5-baud-väckning mottagen.") from None

    def stang(self) -> None:
        self.stangd = True


def _tom(k: queue.Queue[int]) -> None:
    """Töm en kö utan att blockera."""
    while True:
        try:
            k.get_nowait()
        except queue.Empty:
            return


def skapa_lankat_par() -> tuple[Minnestransport, Minnestransport]:
    """Skapa (klienttransport, ecutransport) kopplade till samma virtuella buss."""
    ko_klient: queue.Queue[int] = queue.Queue()
    ko_ecu: queue.Queue[int] = queue.Queue()
    ko_5baud: queue.Queue[int] = queue.Queue()
    klient = Minnestransport(ko_klient, ko_ecu, ko_5baud, True, "minne:klient")
    ecu = Minnestransport(ko_ecu, ko_klient, ko_5baud, False, "minne:ecu")
    return klient, ecu


# ---------------------------------------------------------------------------
# Hjälpfunktioner för portar och FTDI-latens
# ---------------------------------------------------------------------------


def pyserial_finns() -> bool:
    """True om pyserial går att importera."""
    try:
        import serial  # noqa: F401
    except ImportError:
        return False
    return True


def lista_portar() -> list[tuple[str, str]]:
    """Returnera [(port, beskrivning), ...]. Tom lista om pyserial saknas."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description or "") for p in list_ports.comports()]


def _ftdi_nycklar() -> Iterator[tuple[str, object]]:
    """Iterera över (portnamn, öppen registernyckel) för FTDI-enheter (Windows).

    Anroparen ansvarar för att stänga varje utlämnad nyckel.
    """
    if os.name != "nt":  # pragma: no cover - endast Windows
        return
    import winreg

    bas = r"SYSTEM\CurrentControlSet\Enum\FTDIBUS"
    try:
        rot = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, bas)
    except OSError:
        return
    with rot:
        i = 0
        while True:
            try:
                enhet = winreg.EnumKey(rot, i)
            except OSError:
                return
            i += 1
            sokvag = f"{bas}\\{enhet}\\0000\\Device Parameters"
            nyckel = None
            for atkomst in (winreg.KEY_READ | winreg.KEY_SET_VALUE, winreg.KEY_READ):
                try:
                    nyckel = winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, sokvag, 0, atkomst
                    )
                    break
                except OSError:
                    continue
            if nyckel is None:
                continue
            try:
                port = str(winreg.QueryValueEx(nyckel, "PortName")[0])
            except OSError:
                nyckel.Close()
                continue
            yield port, nyckel


def las_ftdi_latens(port: str) -> int | None:
    """Läs FTDI Latency Timer (ms) för given COM-port. None om okänt."""
    if os.name != "nt":  # pragma: no cover - endast Windows
        return None
    import winreg

    for portnamn, nyckel in _ftdi_nycklar():
        try:
            if portnamn.upper() != port.upper():
                continue
            try:
                return int(winreg.QueryValueEx(nyckel, "LatencyTimer")[0])
            except OSError:
                return None
        finally:
            nyckel.Close()  # type: ignore[attr-defined]
    return None


def satt_ftdi_latens(port: str, ms: int = 1) -> bool:
    """Försök sätta FTDI Latency Timer till ``ms``.

    Kräver administratörsrättigheter, och enheten måste kopplas ur/i (eller
    datorn startas om) innan det slår igenom. Returnerar True vid lyckad
    skrivning. Se README för den manuella vägen via Enhetshanteraren.
    """
    if os.name != "nt":  # pragma: no cover - endast Windows
        return False
    import winreg

    for portnamn, nyckel in _ftdi_nycklar():
        try:
            if portnamn.upper() != port.upper():
                continue
            try:
                winreg.SetValueEx(nyckel, "LatencyTimer", 0, winreg.REG_DWORD, ms)
                return True
            except OSError:
                return False
        finally:
            nyckel.Close()  # type: ignore[attr-defined]
    return False


def ar_windows() -> bool:
    """True om vi kör på Windows (används för OS-specifika tips)."""
    return sys.platform.startswith("win")
