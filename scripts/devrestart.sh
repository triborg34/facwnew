#!/usr/bin/env bash
# Dev convenience: stop whatever holds the port, restart, wait for health.
# Delegates to the packaged tool so PID bookkeeping matches production use:
#   python -m facw.tools.service_control restart
set -u
PORT="${1:-${FACW_PORT:-8770}}"
cd "$(dirname "$0")/.." || exit 1
exec python -m facw.tools.service_control --port "$PORT" restart
