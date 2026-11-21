#!/bin/sh
# Records the key map and the tokenizer recordings inside the scorer's pinned image.
# Usage: tools/record_in_image.sh <sqlglot checkout at eb11aec4> [platform] [tests.json] [out dir]
#   platform  linux/arm64 (default, native on Apple Silicon) or linux/amd64 (the scorer's)
#   tests.json  which tests to record (default sample/tests.json)
#   out dir   where keymap.pkl.gz and tokreplay.pkl.xz go (default blast/)
# Seeds 0,1,2 as in tools/build_keymap.py and tools/build_tokreplay.py.
set -e
SRC=$(cd "$1" && pwd)
PLATFORM=${2:-linux/arm64}
REPO=$(cd "$(dirname "$0")/.." && pwd)
TESTS=${3:-sample/tests.json}
OUT=${4:-blast}
JOBS=${JOBS:-10}
docker run --rm --platform "$PLATFORM" -v "$SRC":/src:ro -v "$REPO":/repo -w /repo \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 sh -c "
    pip install -q --root-user-action=ignore -r requirements.lock &&
    cp -r /src /tmp/sqlglot && find /tmp/sqlglot -name __pycache__ -prune -exec rm -rf {} + &&
    python tools/build_keymap.py /tmp/sqlglot $TESTS $OUT/keymap.pkl.gz $JOBS 0,1,2 &&
    python tools/build_tokreplay.py /tmp/sqlglot $TESTS $OUT/tokreplay.pkl.xz $JOBS 0,1,2"
