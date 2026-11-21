#!/bin/sh
# Snapshots sqlglot's state right after import (blast/importstate.py) into <out.pkl>.
# Taken under hash seed 0, as the selector's patched imports are, after three other
# seeds mark the few entries that differ from process to process.
# Usage: tools/build_importstate.sh <sqlglot dir> <out.pkl>
set -e
SRC=$1; OUT=$2
HERE=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
for seed in 1 2 3; do
  PYTHONHASHSEED=$seed PYTHONDONTWRITEBYTECODE=1 python "$HERE/blast/importstate.py" snapshot "$SRC" "$TMP/$seed.pkl"
done
PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 IMPORTSTATE_OTHER="$TMP/1.pkl,$TMP/2.pkl,$TMP/3.pkl" \
  python "$HERE/blast/importstate.py" snapshot "$SRC" "$OUT"
rm -rf "$TMP"
