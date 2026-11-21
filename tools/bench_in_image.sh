#!/bin/sh
# Runs the key-map and tokenizer-replay benches inside the scorer's pinned image:
# planted edits, the selector with and without each stage, and every dropped test
# run on the patched code. Results go to build/bench-keymap and build/bench-tokreplay.
# Usage: tools/bench_in_image.sh <sqlglot checkout at eb11aec4> [platform] [count] [seed]
set -e
SRC=$(cd "$1" && pwd)
PLATFORM=${2:-linux/arm64}
COUNT=${3:-40}
SEED=${4:-5}
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$REPO/build"
docker run --rm --platform "$PLATFORM" -v "$SRC":/src:ro -v "$REPO":/repo -w /repo \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 sh -c "
    pip install -q --root-user-action=ignore -r requirements.lock &&
    cp -r /src /tmp/sqlglot && find /tmp/sqlglot -name __pycache__ -prune -exec rm -rf {} + &&
    sh tools/build_importstate.sh /tmp/sqlglot blast/importstate.pkl &&
    PYTHONHASHSEED=0 python blast/tokreplay.py base /tmp/sqlglot blast/tokreplay.pkl.xz blast/tokreplay_base.pkl &&
    python tools/keymap_bench.py /tmp/sqlglot sample/tests.json /tmp/bench-keymap $COUNT $SEED 8 | tee build/bench-keymap.txt | tail -3 &&
    python tools/tokreplay_bench.py /tmp/sqlglot sample/tests.json /tmp/bench-tokreplay $COUNT $SEED 8 | tee build/bench-tokreplay.txt | tail -3"
