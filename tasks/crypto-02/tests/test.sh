#!/bin/sh
set -eu
exec python3 -I -c "import sys; sys.path.insert(0, '/tests'); from benchmark.verifier import main; main()"
