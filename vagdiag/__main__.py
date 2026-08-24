"""Startpunkt för VAGDIAG.

Exempel::

    python -m vagdiag COM3                     # meny
    python -m vagdiag COM3 --autoscan          # scanna alla styrdon och avsluta
    python -m vagdiag COM3 --felkoder          # läs felkoder ur motorstyrdonet
    python -m vagdiag COM3 --grupper 3 4 11 --logg backe.csv
    python -m vagdiag COM3 --gui               # tkinter-instrumentpanel
    python -m vagdiag --simulator              # kör mot virtuell ECU, utan bil
    python -m vagdiag --lista-portar
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import __version__
from .meny import Meny, forbered_terminal, formatera_felkoder, rubrik, visa_fel
from .transport import (
    Serietransport,
    Transport,
    las_ftdi_latens,
    lista_portar,
    pyserial_finns,
    satt_ftdi_latens,
)
from .undantag import VagdiagFel

__all__ = ["main"]


def bygg_parser() -> argparse.ArgumentParser:
    """Bygg argumentparsern."""
    parser = argparse.ArgumentParser(
        prog="python -m vagdiag",
        description="VAGDIAG – diagnos för äldre VAG-bilar över KWP1281/K-line.",
        epilog="Hobbyverktyg – används på egen risk. VCDS är facit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "port", nargs="?",
        help="serieport till KKL-kabeln, t.ex. COM3 eller /dev/ttyUSB0",
    )
    parser.add_argument(
        "--simulator", action="store_true",
        help="kör mot den inbyggda virtuella ECU:n i stället för en riktig bil",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="starta tkinter-instrumentpanelen i stället för terminalmenyn",
    )

    direkt = parser.add_argument_group("direktkörning utan meny")
    direkt.add_argument(
        "--autoscan", action="store_true", help="scanna alla styrdon och avsluta",
    )
    direkt.add_argument(
        "--felkoder", action="store_true", help="läs felkoder och avsluta",
    )
    direkt.add_argument(
        "--grupper", nargs="+", type=int, metavar="N",
        help="läs mätgrupper live, t.ex. --grupper 3 4 11",
    )
    direkt.add_argument(
        "--styrdon", default="01", metavar="HEX",
        help="styrdonsadress i hex för direktkörning (standard: 01 = motor)",
    )
    direkt.add_argument("--logg", metavar="FIL", help="CSV-fil att logga till")

    ovrigt = parser.add_argument_group("övrigt")
    ovrigt.add_argument(
        "--lista-portar", action="store_true", help="visa tillgängliga serieportar",
    )
    ovrigt.add_argument(
        "--satt-latens", action="store_true",
        help="försök sätta FTDI Latency Timer till 1 ms (Windows, kräver admin)",
    )
    ovrigt.add_argument(
        "--timeout", type=float, default=1.0, metavar="S",
        help="timeout per byte i sekunder (standard: 1.0)",
    )
    ovrigt.add_argument(
        "--init-timeout", type=float, default=2.0, metavar="S",
        help="timeout för svar på 5-baud-init (standard: 2.0)",
    )
    ovrigt.add_argument(
        "--intervall", type=float, default=0.0, metavar="S",
        help="paus mellan avläsningsvarv i liveläget (standard: 0 = så snabbt det går)",
    )
    ovrigt.add_argument(
        "--csv-punkt", action="store_true",
        help="skriv CSV med komma och decimalpunkt i stället för svensk Excel-stil",
    )
    ovrigt.add_argument(
        "--debug", action="store_true", help="skriv all blocktrafik till stderr",
    )
    ovrigt.add_argument("--version", action="version", version=f"VAGDIAG {__version__}")
    return parser


def _visa_portar() -> int:
    """Lista serieportar med FTDI-latens där den går att läsa."""
    if not pyserial_finns():
        print("pyserial är inte installerat. Kör: pip install pyserial")
        return 1
    portar = lista_portar()
    if not portar:
        print("Inga serieportar hittades.")
        print("Koppla in KKL-kabeln och kontrollera att den syns i")
        print("Enhetshanteraren under \"Portar (COM & LPT)\".")
        return 1
    print(rubrik("SERIEPORTAR"))
    for namn, beskrivning in portar:
        latens = las_ftdi_latens(namn)
        latenstext = ""
        if latens is not None:
            latenstext = f"  [FTDI Latency Timer: {latens} ms" + (
                "]" if latens <= 2 else " – SÄTT TILL 1!]"
            )
        print(f"  {namn:<10} {beskrivning}{latenstext}")
    return 0


def _satt_latens(port: str | None) -> int:
    """Försök sätta latensen till 1 ms för angiven port."""
    if not port:
        print("Ange vilken port det gäller, t.ex.: "
              "python -m vagdiag COM3 --satt-latens")
        return 2
    if satt_ftdi_latens(port, 1):
        print(f"Latency Timer för {port} satt till 1 ms.")
        print("Koppla ur och i USB-kabeln (eller starta om) för att det ska gälla.")
        return 0
    print(f"Kunde inte skriva Latency Timer för {port}.")
    print("Kör terminalen som administratör, eller sätt den manuellt:")
    print("  Enhetshanteraren -> Portar (COM & LPT) -> USB Serial Port ->")
    print("  Egenskaper -> Portinställningar -> Avancerat -> Latency Timer = 1")
    return 1


def _oppna_transport(args: argparse.Namespace) -> tuple[Transport, object | None]:
    """Öppna transporten. Returnerar (transport, simulatorkoppling eller None)."""
    if args.simulator:
        from .simulator import starta_simulator

        koppling = starta_simulator()
        print("Kör mot den virtuella ECU:n (ingen bil inblandad).")
        return koppling.transport, koppling
    if not args.port:
        raise VagdiagFel(
            "Ingen serieport angiven.",
            tips="Kör 'python -m vagdiag --lista-portar' för att se dina portar, "
            "eller 'python -m vagdiag --simulator' för att prova utan bil.",
        )
    return Serietransport(
        args.port, timeout=args.timeout,
    ), None


def _direktkorning(meny: Meny, args: argparse.Namespace) -> int:
    """Utför --autoscan/--felkoder/--grupper utan att visa menyn."""
    if args.autoscan:
        meny.autoscan()
        return 0

    try:
        adress = int(args.styrdon, 16)
    except ValueError:
        print(f"Ogiltig styrdonsadress: {args.styrdon}")
        return 2

    meny.anslut(adress)
    klient = meny.klient
    assert klient is not None

    if args.felkoder:
        print(rubrik("FELKODER"))
        for rad in formatera_felkoder(klient.las_felkoder()):
            print(rad)
        return 0

    if args.grupper:
        grupper = [g for g in args.grupper if 1 <= g <= 255]
        if not grupper:
            print("Inga giltiga grupper angivna (1-255).")
            return 2
        meny.live(klient, grupper, args.logg)
        return 0
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Kör VAGDIAG. Returnerar processens exitkod."""
    forbered_terminal()
    parser = bygg_parser()
    args = parser.parse_args(argv)

    if args.lista_portar:
        return _visa_portar()
    if args.satt_latens:
        return _satt_latens(args.port)

    spar = None
    if args.debug:
        def spar(text: str) -> None:
            print(text, file=sys.stderr)

    koppling = None
    try:
        transport, koppling = _oppna_transport(args)
    except VagdiagFel as fel:
        visa_fel(fel)
        return 2

    meny = Meny(
        transport,
        spar=spar,
        avgransare="," if args.csv_punkt else ";",
        decimalkomma=not args.csv_punkt,
        intervall=args.intervall,
        timeout=args.timeout,
        init_timeout=args.init_timeout,
    )

    try:
        if args.gui:
            from .gui import kor_gui

            try:
                gui_adress = int(args.styrdon, 16)
            except ValueError:
                print(f"Ogiltig styrdonsadress: {args.styrdon}")
                return 2
            gui_grupper = [g for g in (args.grupper or [3, 11]) if 1 <= g <= 255]
            return kor_gui(meny, gui_adress, gui_grupper or [3, 11])
        if args.autoscan or args.felkoder or args.grupper:
            return _direktkorning(meny, args)
        return meny.kor()
    except VagdiagFel as fel:
        visa_fel(fel)
        return 2
    except KeyboardInterrupt:
        print("\nAvbrutet.")
        return 130
    finally:
        meny.koppla_ner()
        if koppling is not None:
            koppling.stang()  # type: ignore[attr-defined]
        else:
            transport.stang()


if __name__ == "__main__":
    raise SystemExit(main())
