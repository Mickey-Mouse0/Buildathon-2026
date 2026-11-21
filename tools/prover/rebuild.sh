#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOLS="$REPO/tools/prover"
BUILD="${BUILD:-$REPO/build}"
SEEDS="${SEEDS:-0 1 2}"
JOBS="${JOBS:-8}"
PYTHON313="${PYTHON313:-python3.13}"

mkdir -p "$BUILD"
BUILD="$(cd "$BUILD" && pwd)"
SQLGLOT="$BUILD/sqlglot"
VENV="$BUILD/venv"
PY="$VENV/bin/python"

step() { printf '\n== %s\n' "$*"; }

step "sqlglot at the pinned commit -> $SQLGLOT"
[ -d "$SQLGLOT/sqlglot" ] || python3 "$REPO/tools/fetch_sqlglot.py" "$SQLGLOT"

step "Python 3.13 environment from requirements.lock -> $VENV"
if [ ! -x "$PY" ]; then
  "$PYTHON313" -m venv "$VENV"
  "$PY" -m pip install -q -r "$REPO/requirements.lock" coverage pytest-cov
fi

step "1/8 whole suite with per-test coverage, file reads and dialect use"
rm -rf "$BUILD/fixtures" "$BUILD/.coverage"
(cd "$SQLGLOT" && SKIP_INTEGRATION=0 PATH="$VENV/bin:$PATH" PYTHONPATH=".:$TOOLS" \
  FIXTURE_TRACKER_OUT="$BUILD/fixtures" COVERAGE_FILE="$BUILD/.coverage" \
  "$PY" -m pytest -q -n "$JOBS" -p fixture_tracker -p no:cacheprovider \
  --cov=sqlglot --cov-context=test --cov-report= tests | tail -2)

step "2/8 line map"
"$PY" "$TOOLS/build_line_map.py" "$BUILD/.coverage" "$BUILD/fixtures" "$REPO/sample/tests.json" "$SQLGLOT" "$BUILD/map.json"

step "3/8 instrumented copy of sqlglot"
"$PY" "$TOOLS/instrument.py" "$SQLGLOT" "$BUILD/instr" "$BUILD/target_index.json"
mkdir -p "$BUILD/runroot"
ln -sfn "$SQLGLOT/tests" "$BUILD/runroot/tests"
ln -sfn "$BUILD/instr/sqlglot" "$BUILD/runroot/sqlglot"

step "4/8 value profiles of the heavy tests, seeds: $SEEDS"
DIRS=()
for seed in $SEEDS; do
  PROVER_BUILD="$BUILD" PROFILE_OUT="$BUILD/profiles-s$seed" PROFILE_SEED="$seed" \
    "$PY" "$TOOLS/profile_heavy.py" "$REPO/sample/tests.json" 0.3 "$JOBS"
  DIRS+=("$BUILD/profiles-s$seed")
done

step "5/8 package into blast/prover_data"
"$PY" "$TOOLS/package_data.py" "$REPO/sample/tests.json" "$BUILD/map.json" "$(IFS=,; echo "${DIRS[*]}")" \
  "$BUILD/target_index.json" "$SQLGLOT" "$REPO/blast/prover_data"

step "6/8 value profiles of every other test (one fork per test), merged into union.pkl.xz"
UDIRS=()
for seed in $SEEDS; do
  "$PY" "$TOOLS/profile_all.py" "$REPO/sample/tests.json" "$BUILD/runroot" "$BUILD/all-s$seed" "$seed" "$JOBS"
  UDIRS+=("$BUILD/all-s$seed")
done
"$PY" -c "import lzma, shutil; shutil.copyfileobj(lzma.open('$REPO/blast/prover_data/profiles.pkl.xz'), open('$REPO/blast/prover_data/profiles.pkl', 'wb'))"
"$PY" "$TOOLS/package_union.py" "$REPO/sample/tests.json" "$(IFS=,; echo "${UDIRS[*]}")" "$REPO/blast/prover_data/profiles.pkl" \
  "$BUILD/target_index.json" "$SQLGLOT" "$REPO/blast/prover_data/union.pkl.xz"

step "7/8 re-run each heavy test alone and merge every line and file it touches"
"$PY" "$TOOLS/isolated_maps.py" "$REPO/blast/prover_data/heavy_map.json.gz" "$REPO/sample/tests.json" \
  "$SQLGLOT" "$BUILD/isolated" "$REPO/blast/prover_data/heavy_map.json.gz" "$JOBS"

step "8/8 lemma checks"
"$PY" -c "import lzma, shutil; shutil.copyfileobj(lzma.open('$REPO/blast/prover_data/union.pkl.xz'), open('$REPO/blast/prover_data/union.pkl', 'wb'))"
"$PY" "$TOOLS/lemma_checks.py" "$SQLGLOT"
