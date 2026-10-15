#!/bin/sh
# The harness copies a fresh flag to /workspace/flag.txt after startup; the
# internal service reads it per request. The gateway listens on 3000; the
# middleman and the internal service bind loopback only.
set -eu
cd /app
exec node support/server.js
