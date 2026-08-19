#!/bin/sh
# Minimal Railway command wrapper. All live Hermes state stays on /opt/data.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
export HERMES_HOME

if [ "$(id -u)" -eq 0 ]; then
    exec s6-setuidgid hermes "$HERMES_BIN" gateway run
fi
exec "$HERMES_BIN" gateway run
