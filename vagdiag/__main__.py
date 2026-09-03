"""Entry point for VAGDIAG.

Examples::

    python -m vagdiag COM3                    # menu
    python -m vagdiag COM3 --autoscan         # scan every module and exit
    python -m vagdiag COM3 --faults           # read the engine fault codes
    python -m vagdiag COM3 --groups 3 4 11 --log hill.csv
    python -m vagdiag COM3 --gui              # tkinter dashboard
    python -m vagdiag --simulator             # run against the virtual ECU
    python -m vagdiag --list-ports
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import __version__
from .exceptions import VagdiagError
from .menu import Menu, format_faults, heading, setup_terminal, show_error
from .transport import (
    SerialTransport,
    Transport,
    list_ports,
    pyserial_available,
    read_ftdi_latency,
    set_ftdi_latency,
)

__all__ = ["main", "main_gui"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m vagdiag",
        description="VAGDIAG - diagnostics for older VAG cars over KWP1281/K-line.",
        epilog="Hobby tool - use at your own risk. VCDS is the reference.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "port", nargs="?",
        help="serial port of the KKL cable, for example COM3 or /dev/ttyUSB0",
    )
    parser.add_argument(
        "--simulator", action="store_true",
        help="run against the built-in virtual ECU instead of a real car",
    )
    parser.add_argument(
        "--gui", action="store_true",
        help="start the tkinter dashboard instead of the terminal menu",
    )

    direct = parser.add_argument_group("direct commands (no menu)")
    direct.add_argument(
        "--autoscan", action="store_true", help="scan every module and exit",
    )
    direct.add_argument(
        "--faults", action="store_true", help="read the fault codes and exit",
    )
    direct.add_argument(
        "--groups", nargs="+", type=int, metavar="N",
        help="live view of measuring blocks, for example --groups 3 4 11",
    )
    direct.add_argument(
        "--module", default="01", metavar="HEX",
        help="module address in hex for direct commands (default: 01 = engine)",
    )
    direct.add_argument("--log", metavar="FILE", help="CSV file to log into")

    other = parser.add_argument_group("other")
    other.add_argument(
        "--list-ports", action="store_true", help="show the available serial ports",
    )
    other.add_argument(
        "--set-latency", action="store_true",
        help="try to set the FTDI latency timer to 1 ms (Windows, needs admin)",
    )
    other.add_argument(
        "--timeout", type=float, default=1.0, metavar="S",
        help="per-byte timeout in seconds (default: 1.0)",
    )
    other.add_argument(
        "--baud", type=int, default=None, metavar="N",
        help="force the K-line baud rate (10400 or 9600). By default the "
        "program tries 10400 and falls back to 9600 on its own",
    )
    other.add_argument(
        "--init-timeout", type=float, default=2.0, metavar="S",
        help="timeout for the answer to the 5-baud init (default: 2.0)",
    )
    other.add_argument(
        "--interval", type=float, default=0.0, metavar="S",
        help="pause between polling cycles in the live view (default: 0 = as fast "
        "as possible)",
    )
    other.add_argument(
        "--csv-dot", action="store_true",
        help="write CSV with commas and a decimal point instead of the "
        "European Excel style",
    )
    other.add_argument(
        "--debug", action="store_true", help="trace all block traffic to stderr",
    )
    other.add_argument("--version", action="version", version=f"VAGDIAG {__version__}")
    return parser


def _show_ports() -> int:
    """List the serial ports, with the FTDI latency where it can be read."""
    if not pyserial_available():
        print("pyserial is not installed. Run: pip install pyserial")
        return 1
    ports = list_ports()
    if not ports:
        print("No serial ports found.")
        print("Plug in the KKL cable and check that it shows up in Device")
        print("Manager under \"Ports (COM & LPT)\".")
        return 1
    print(heading("SERIAL PORTS"))
    for name, description in ports:
        latency = read_ftdi_latency(name)
        latency_text = ""
        if latency is not None:
            latency_text = f"  [FTDI latency timer: {latency} ms" + (
                "]" if latency <= 2 else " - SET IT TO 1!]"
            )
        print(f"  {name:<10} {description}{latency_text}")
    return 0


def _set_latency(port: str | None) -> int:
    """Try to set the latency timer to 1 ms for the given port."""
    if not port:
        print("Name the port, for example: python -m vagdiag COM3 --set-latency")
        return 2
    if set_ftdi_latency(port, 1):
        print(f"Latency timer for {port} set to 1 ms.")
        print("Unplug and replug the USB cable (or reboot) for it to take effect.")
        return 0
    print(f"Could not write the latency timer for {port}.")
    print("Run the terminal as administrator, or set it manually:")
    print("  Device Manager -> Ports (COM & LPT) -> USB Serial Port ->")
    print("  Properties -> Port Settings -> Advanced -> Latency Timer = 1")
    return 1


def _open_transport(args: argparse.Namespace) -> tuple[Transport, object | None]:
    """Open the transport. Returns (transport, simulator link or None)."""
    if args.simulator:
        from .simulator import start_simulator

        link = start_simulator()
        print("Running against the virtual ECU (no car involved).")
        return link.transport, link
    if not args.port:
        raise VagdiagError(
            "No serial port given.",
            hint="Run 'python -m vagdiag --list-ports' to see your ports, or "
            "'python -m vagdiag --simulator' to try it without a car.",
        )
    return SerialTransport(args.port, baud=args.baud, timeout=args.timeout), None


def _run_direct(menu: Menu, args: argparse.Namespace) -> int:
    """Run --autoscan/--faults/--groups without showing the menu."""
    if args.autoscan:
        menu.autoscan()
        return 0

    try:
        address = int(args.module, 16)
    except ValueError:
        print(f"Invalid module address: {args.module}")
        return 2

    menu.connect(address)
    client = menu.client
    assert client is not None

    if args.faults:
        print(heading("FAULT CODES"))
        for line in format_faults(client.read_faults()):
            print(line)
        return 0

    if args.groups:
        groups = [g for g in args.groups if 1 <= g <= 255]
        if not groups:
            print("No valid groups given (1-255).")
            return 2
        menu.live(client, groups, args.log)
        return 0
    return 0


def _run_gui(args: argparse.Namespace, trace) -> int:
    """Start the desktop application, prefilled from the command line."""
    from .gui import run_gui
    from .guiworker import Settings

    try:
        address = int(args.module, 16)
    except ValueError:
        print(f"Invalid module address: {args.module}")
        return 2
    groups = [g for g in (args.groups or [3, 11]) if 1 <= g <= 255] or [3, 11]
    settings = Settings(
        timeout=args.timeout,
        init_timeout=args.init_timeout,
        interval=args.interval,
        delimiter="," if args.csv_dot else ";",
        decimal_comma=not args.csv_dot,
        trace=trace,
    )
    return run_gui(settings, args.port, args.simulator, address, groups)


def main(argv: Sequence[str] | None = None) -> int:
    """Run VAGDIAG. Returns the process exit code."""
    setup_terminal()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_ports:
        return _show_ports()
    if args.set_latency:
        return _set_latency(args.port)

    trace = None
    if args.debug:
        def trace(text: str) -> None:
            print(text, file=sys.stderr)

    if args.gui:
        return _run_gui(args, trace)

    link = None
    try:
        transport, link = _open_transport(args)
    except VagdiagError as error:
        show_error(error)
        return 2

    menu = Menu(
        transport,
        trace=trace,
        delimiter="," if args.csv_dot else ";",
        decimal_comma=not args.csv_dot,
        interval=args.interval,
        timeout=args.timeout,
        init_timeout=args.init_timeout,
    )

    try:
        if args.autoscan or args.faults or args.groups:
            return _run_direct(menu, args)
        return menu.run()
    except VagdiagError as error:
        show_error(error)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        menu.disconnect()
        if link is not None:
            link.close()  # type: ignore[attr-defined]
        else:
            transport.close()


def main_gui(argv: Sequence[str] | None = None) -> int:
    """Entry point for the desktop shortcut - always starts the window."""
    arguments = list(argv if argv is not None else sys.argv[1:])
    if "--gui" not in arguments:
        arguments.append("--gui")
    return main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
