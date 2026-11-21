#!/bin/sh
# Runs sqlglot's whole test suite once with the coverage plugin and writes the map.
# Usage: tools/run_coverage.sh <sqlglot_src> <out.pkl.gz>
# Run it inside the pinned python image with requirements.lock installed (see tools/build_map_docker.sh).
set -e
SRC=$(cd "$1" && pwd)
OUT=$2
TOOLS=$(cd "$(dirname "$0")" && pwd)
RAW=$(mktemp -d)
cd "$SRC"
SKIP_INTEGRATION=0 PYTHONPATH="$SRC:$TOOLS" COVMAP_ROOT="$SRC" COVMAP_OUT="$RAW" \
  python -m pytest tests -q -p no:cacheprovider -p covmap_plugin -n "${JOBS:-4}" 2>&1 | tail -3
cd - > /dev/null
python "$TOOLS/build_map.py" "$RAW" "$SRC" "$OUT"
