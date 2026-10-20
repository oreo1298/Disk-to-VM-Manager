#!/bin/bash
# Screenshot tour of the GUI on a virtual X server (no desktop needed).
#
#   tests/gui/tour.sh [SCENARIO.py] [OUTPUT-DIR]
#
# Uses the VM library in $REHOME_HOME (default: a throw-away one). Needs Xvfb
# and dbus-run-session. Screenshots are written as PNG files.
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
SCENARIO=$(readlink -f "${1:-$HERE/scenario_tour.py}")
export SHOTS=$(readlink -m "${2:-$PWD/gui-shots}")
mkdir -p "$SHOTS"
export REHOME_HOME=${REHOME_HOME:-$(mktemp -d)} REHOME_NO_FIRST_RUN=1 GDK_BACKEND=x11
export GSK_RENDERER=${GSK_RENDERER:-cairo} NO_AT_BRIDGE=1
D=${DISPLAY_NUM:-$((90 + RANDOM % 100))}
Xvfb ":$D" -screen 0 1400x900x24 >/dev/null 2>&1 &
XPID=$!
trap 'kill $XPID 2>/dev/null' EXIT
for _ in $(seq 50); do [ -e "/tmp/.X11-unix/X$D" ] && break; python3 -c 'import time; time.sleep(0.1)'; done
DISPLAY=":$D" dbus-run-session -- "${PYTHON:-python3}" "$HERE/drive.py" "$SCENARIO"
echo "Screenshots in $SHOTS"
