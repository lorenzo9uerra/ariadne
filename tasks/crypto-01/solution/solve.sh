#!/bin/sh
# Harbor Oracle entrypoint. The recovery algorithm is staged beside this script.
set -eu
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
python3 "$here/recover.py" > "$here/stdout.txt"
cp "$here/stdout.txt" /logs/artifacts/submission.json
rm -f "$here/stdout.txt"
printf '%s\n' 'reference finished'
