#!/bin/sh
# Rebuilds blast/covmap.pkl.gz inside the pinned base image, so Python and SQLite
# match the image the answers were labeled in.
# Usage: tools/build_map_docker.sh <path to sqlglot checkout at eb11aec4> [wheel dir]
set -e
SRC=$(cd "$1" && pwd)
REPO=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
if [ -n "$2" ]; then
  WHEELS=$(cd "$2" && pwd)
  INSTALL="pip install -q --no-index --find-links /wheels -r /repo/requirements.lock"
  MOUNT="-v $WHEELS:/wheels:ro"
else
  INSTALL="pip install -q -r /repo/requirements.lock"
  MOUNT=""
fi
docker run --rm --platform linux/amd64 $MOUNT -v "$SRC":/src:ro -v "$REPO":/repo \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 sh -c "
    $INSTALL && cp -r /src /tmp/sqlglot && find /tmp/sqlglot -name __pycache__ -prune -exec rm -rf {} + &&
    sh /repo/tools/run_coverage.sh /tmp/sqlglot /repo/blast/covmap.pkl.gz"
