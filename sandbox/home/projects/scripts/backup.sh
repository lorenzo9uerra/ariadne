#!/bin/sh
# Copy the projects folder into a dated archive under ~/backups.
set -eu
target="$HOME/backups/projects-$(date +%Y%m%d).tar.gz"
if [ "${1:-}" = "--dry-run" ]; then
    echo "would write $target"
    exit 0
fi
mkdir -p "$HOME/backups"
tar -czf "$target" -C "$HOME" projects
echo "wrote $target"
