#!/bin/bash
set -eu
cd "$(dirname "$0")"
exec uv run python -m benchmark.runner compare "$@"
