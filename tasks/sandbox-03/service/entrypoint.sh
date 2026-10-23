#!/bin/sh
# The harness stages a fresh flag into /workspace after startup; the runner
# does not read it. The exploit's host access does.
set -eu
exec node /app/server.js
