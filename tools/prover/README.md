# Prover tooling

Everything used to build and check the prover's data in `blast/prover_data/`. None of it runs in the submitted image. The image only reads the packaged files. How the prover works is described in `APPROACH.md`.

## Rebuild the data

You need Python 3.13 (`python3.13` on your PATH, or set `PYTHON313`) and about 2 GB of disk space.

```bash
tools/prover/rebuild.sh
```

This takes 25 to 40 minutes on 8 cores. Most of the time goes to `test_max_nodes`, which takes about 8 minutes under the recorder, once per seed. Everything goes into `build/`, which is gitignored, and the results overwrite `blast/prover_data/`. Commit those three files afterwards.

Optional settings: `BUILD=...` (work folder), `SEEDS="0 1 2"` (hash seeds to record under), `JOBS=8`.

| Step | Script | What it makes |
| --- | --- | --- |
| 1 | `fixture_tracker.py` (pytest plugin) | Runs the whole suite once. Records per-test coverage, which files each test opens, and which dialects it uses. |
| 2 | `build_line_map.py` | `build/map.json`: line → tests, lines that run at import, file → tests |
| 3 | `instrument.py` | A copy of sqlglot where every condition and operator expression reports its inputs and result. Also `target_index.json`. |
| 4 | `profile_heavy.py` + `value_recorder.py` | Runs each test of 0.3 s or more alone under the instrumented copy, once per seed. Writes one pickle per test. |
| 5 | `package_data.py` | Merges the seeds and keys everything by test id. Writes `blast/prover_data/`. |
| 6 | `profile_all.py` + `package_union.py` | Records every other test the same way. Each test runs in a fork of one post-import process, so no test sees another's state. The records of all tests are merged per expression into `union.pkl.xz`. |
| 7 | `isolated_maps.py` | Re-runs each heavy test alone with coverage and file tracking, and merges what it touches into the map. This catches reads in `setUpClass` and code that a shared run skips because of warm caches. |
| 8 | `lemma_checks.py` | Hand-written edge cases (see below). It must print `0 failing checks`. |

## Check the prover

**Lemma checks**, about 5 seconds:
```bash
build/venv/bin/python tools/prover/lemma_checks.py build/sqlglot
```
These are 17 hand-made patches with a known right answer: comment-only edits, patches without a header, coding cookies, TPC fixture edits, edits to `test_dialect.py` (which other test files import), added files, syntax errors, and zero-context hunks. Add a case here whenever you find a new kind of bug.

**Whole-commit claims**: `bench.py ... --union --coverage build/map.json` runs every test covering the changed file for each patch the all-tests replay calls a no-op.

**Mutant benches** plant a small bug, ask the prover which heavy tests it can skip, then actually run those tests on the changed code. A test that fails after the prover called it safe would have cost 300 s on the board. The bench prints these as `VIOLATION`.

```bash
# planted bugs in sqlglot code: and/or, comparisons, constants, not, deleted statements, comments, docstrings
build/venv/bin/python tools/prover/mutants.py build/sqlglot build/map.json build/profiles-s0 build/bench 150 7

# planted bugs in data files and shared test files: fixtures, test_dialect.py, helpers.py
build/venv/bin/python tools/prover/data_mutants.py build/sqlglot build/databench 30 11

# verify; use --check-all for databench, because fixture edits touch no sqlglot lines
BENCH_PYTHON=build/venv/bin/python build/venv/bin/python tools/prover/bench.py \
  --bench build/bench --tests sample/tests.json --sqlglot build/sqlglot --packaged blast/prover_data --jobs 6
```

The bench runs real tests, so it is slow: up to about 90 s per mutant when the prover drops many heavy tests. Results go to `<bench>/results.json`.

The mutants are only for testing. Never ship anything learned from them: a table from a kind of edit to the tests it breaks is against the rules.

## Known limits

- Profiles are recorded on the machine you rebuild on, not inside the scorer's image. A test that takes a different path in the scorer's environment could reach inputs we never recorded.
- Each heavy test is recorded alone. A test that behaves differently after another test in the same process is not covered.
- `test_lazy_load` starts its own `python` subprocess, which has no recorder, so the prover never claims it.
