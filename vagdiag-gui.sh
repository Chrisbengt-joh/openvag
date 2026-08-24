#!/bin/sh
# Start the VAGDIAG window from a file manager or the desktop.
cd "$(dirname "$0")" || exit 1
exec python3 -m vagdiag --gui "$@"
