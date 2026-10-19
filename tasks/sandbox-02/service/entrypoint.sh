#!/bin/sh
# The harness copies a fresh flag to /workspace/flag.txt after startup; the
# service never reads it.
set -eu
exec python3 /app/agent_workspace.py
