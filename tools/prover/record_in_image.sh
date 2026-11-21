#!/bin/sh
# Records all prover data inside the scorer's pinned image (see CLAUDE.md).
# Usage: tools/prover/record_in_image.sh <sqlglot checkout at eb11aec4> <out dir> [platform]
#   out dir   receives profiles.pkl.xz, union.pkl.xz, heavy_map.json.gz, target_index.json.gz
#   platform  linux/arm64 (default, native on Apple Silicon) or linux/amd64 (the scorer's)
set -e
SRC=$(cd "$1" && pwd)
mkdir -p "$2"
OUT=$(cd "$2" && pwd)
PLATFORM=${3:-linux/arm64}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
JOBS=${JOBS:-10}
docker run --rm --platform "$PLATFORM" -v "$SRC":/src:ro -v "$REPO":/repo:ro -v "$OUT":/out -w /repo \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 sh -c "
    set -e
    pip install -q --root-user-action=ignore -r requirements.lock coverage pytest-cov
    cp -r /src /tmp/sqlglot && find /tmp/sqlglot -name __pycache__ -prune -exec rm -rf {} +
    T=/repo/tools/prover; B=/tmp/build; mkdir -p \$B
    (cd /tmp/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=.:\$T FIXTURE_TRACKER_OUT=\$B/fixtures COVERAGE_FILE=\$B/.coverage \
      python -m pytest -q -n $JOBS -p fixture_tracker -p no:cacheprovider --cov=sqlglot --cov-context=test --cov-report= tests | tail -1)
    python \$T/build_line_map.py \$B/.coverage \$B/fixtures sample/tests.json /tmp/sqlglot \$B/map.json
    python \$T/instrument.py /tmp/sqlglot \$B/instr \$B/target_index.json
    mkdir -p \$B/runroot && ln -s /tmp/sqlglot/tests \$B/runroot/tests && ln -s \$B/instr/sqlglot \$B/runroot/sqlglot
    for s in 0 1 2; do
      PROVER_BUILD=\$B PROFILE_OUT=\$B/profiles-s\$s PROFILE_SEED=\$s python \$T/profile_heavy.py sample/tests.json 0.3 2 > \$B/heavy-s\$s.log 2>&1 &
      python \$T/profile_all.py sample/tests.json \$B/runroot \$B/all-s\$s \$s 2 > \$B/all-s\$s.log 2>&1 &
    done
    wait
    cat \$B/heavy-s*.log | grep -v exit=0 || true
    tail -qn1 \$B/all-s*.log
    python \$T/package_data.py sample/tests.json \$B/map.json \$B/profiles-s0,\$B/profiles-s1,\$B/profiles-s2 \$B/target_index.json /tmp/sqlglot /out
    python -c \"import lzma, shutil; shutil.copyfileobj(lzma.open('/out/profiles.pkl.xz'), open('\$B/profiles.pkl', 'wb'))\"
    python \$T/package_union.py sample/tests.json \$B/all-s0,\$B/all-s1,\$B/all-s2 \$B/profiles.pkl \$B/target_index.json /tmp/sqlglot /out/union.pkl.xz
    python \$T/isolated_maps.py /out/heavy_map.json.gz sample/tests.json /tmp/sqlglot \$B/isolated /out/heavy_map.json.gz $JOBS > /dev/null
    echo recorded in image"
