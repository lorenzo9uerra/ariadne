#!/bin/sh
# Run from /workspace in a separate session so child signals cannot reach socat.
# --wait keeps socat attached; without it, the connection closes early.
set -eu
cd /workspace
exec /usr/bin/setsid --wait /usr/bin/timeout --signal=KILL 60s \
    /srv/app/ld-linux-x86-64.so.2 --library-path /srv/app /srv/app/run
