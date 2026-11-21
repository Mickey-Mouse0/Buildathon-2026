#!/bin/sh
# Rebuilds blast/tpc_map.pkl.gz (per-query coverage of the TPC executor tests, and of
# each query's back half) inside the pinned base image.
# Usage: tools/build_tpc_map_docker.sh <path to sqlglot checkout at eb11aec4>
# DOCKER_ARGS adds docker run options (a proxy, a CA bundle). IMAGE overrides the image,
# for an image built FROM the pinned one when the registry can't be reached.
set -e
SRC=$(cd "$1" && pwd)
REPO=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${IMAGE:-python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90}
docker run --rm --platform linux/amd64 $DOCKER_ARGS -v "$SRC":/src:ro -v "$REPO":/repo \
  "$IMAGE" sh -c "
    pip install -q --root-user-action=ignore -r /repo/requirements.lock &&
    cp -r /src /tmp/sqlglot && find /tmp/sqlglot -name __pycache__ -prune -exec rm -rf {} + &&
    PYTHONDONTWRITEBYTECODE=1 python /repo/blast/tpcmap.py map /tmp/sqlglot /repo/blast/tpc_map.pkl.gz"
