#!/usr/bin/env bash
# Build the always-on-top coach overlay.
#
# Usage: ./build.sh, then run overlay/.build/release/coach-overlay while
# `salescoach serve` is running. COACH_URL overrides the server base URL
# (default http://127.0.0.1:8140). Ctrl-C or SIGTERM quits it.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
echo "$(pwd)/.build/release/coach-overlay"
