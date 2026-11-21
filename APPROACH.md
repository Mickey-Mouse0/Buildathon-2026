# Our approach

`blast/selector.py` picks, for each patch, only the tests that can see the change.
It runs in about half a second of CPU and needs no network.

## Pieces

| File | What it does |
| --- | --- |
| `blast/covmap.pkl.gz` | The map, built once from sqlglot's own test run at the pinned commit. For every test: which lines of sqlglot it ran, which data files it opened, and which class settings (class attributes) it read. |
| `blast/analyze.py` | The static analyzer. Works out what kind of code each changed line is, then asks the map which tests can see it. |
| `blast/patchutil.py` | Reads unified diffs and applies them in Python (the image has no `patch` or `git`). |
| `blast/selector.py` | Entry point. Prints one JSON line a commit and picks the cheapest split over 1 to 8 workers. |
| `tools/covmap_plugin.py` | pytest plugin that records the map. |
| `tools/build_map_docker.sh` | Rebuilds the map inside the pinned base image. |

## How a change is matched to tests

- **No real change:** the old and new file parse to the same Python syntax tree (only comments or blank lines changed). Run nothing.
- **Inside a function:** tests that ran the changed statement. A multi-line statement counts as a whole, because an `and` to `or` flip on line 3 changes what line 1 does.
- **A def line, decorator or default value:** tests that ran the function.
- **A lambda inside a table** (like a parser's `FUNCTIONS`): tests that ran the lambda.
- **A class setting** (`WEEK_OFFSET = -1`): tests that read that attribute. Settings copied from it in other class bodies (`{**Parser.FUNCTIONS, ...}`) count too. If code that runs at import (metaclass, `__init_subclass__`) reads it, every test that uses the class.
- **A module constant:** tests that ran a function mentioning its name.
- **A test data file:** tests that opened it.
- **A test file:** the tests in it.
- **Anything unclear** (import lines, syntax errors, class headers): run everything.

## How the map is recorded

`tools/covmap_plugin.py` uses Python 3.13's `sys.monitoring` to record each line once per test. Two things it handles:
- Tests that send work to a process pool (the optimizer and executor tests) would hide that work in child processes. The plugin swaps the pool for an in-process one that still pickles arguments and results, so the pickling code is recorded too.
- Class settings are wrapped in a small recorder after import, so reading `TSQL.WEEK_OFFSET` during a test is noted.

To rebuild: `tools/build_map_docker.sh <sqlglot checkout at eb11aec4>`. It takes about a minute on 4 cores.

## Scores

| | Simulated on 30 samples | Misses |
| --- | --- | --- |
| Starter (all tests, 3 workers) | 1,210.5 s | 0 |
| Selector alone | 170.2 s | 0 |
| Selector + prover | 124.9 s | 0 |
| Perfect answer | 94.4 s | 0 |

## Stage 2: the prover

The selector says which tests *can see* a change. Seeing it is not enough to fail: most changes the heavy tests see do not alter what they compute. `blast/prover/` removes a heavy test only when it can prove the test's outcome cannot change. When it cannot prove that, it keeps the test.

It only reasons about the 31 tests that take 0.3 s or more (87 of the 103 recorded seconds), because those are the only ones worth the effort.

**Value profiles.** `tools/prover/instrument.py` rewrites sqlglot so that every condition and operator expression records the values it sees. Each heavy test then runs alone under that recorder (`tools/prover/profile_heavy.py`). For every expression, we keep the distinct input values and the result it produced in that test. The profiles are built from the pinned commit only and ship in `blast/prover_data/`, compressed.

**Lemmas.** For each heavy test, every change in the patch must be covered by one of these. Otherwise the test stays.

| Lemma | Why it is sound |
| --- | --- |
| The file's syntax tree is unchanged (comments, blank lines) | Nothing in sqlglot or its tests reads line numbers or comments. `test_typing` reads source files but compares syntax trees. |
| Only a docstring changed (not in `sqlglot/typing/`) | Nothing reads `__doc__`. `test_typing` compares code constants, so `typing/` is excluded. |
| The changed expression was never evaluated by the test | The test's run never reached it, so it cannot reach the new one. |
| The changed statement, inside a function body, never ran in the test and does not run at import | Same argument, using the line map. |
| Replaying the new expression on every recorded input gives the same value as the old one | A run only diverges when a changed expression produces a different value. Up to that point the run is the original one, so every input is a recorded one. |

The replay is conservative. Objects it cannot evaluate are kept opaque: only their truthiness, identity and whether `==` is plain identity are known. Anything needing more is "unknown", and the test stays. Examples it proves: `x is E.A` to `x == E.A` on an enum, `self._table is None` to `== None` on a class without `__eq__`, and an `and`/`or` swap that short-circuits the same way on every recorded input.

**Checked on planted bugs.** `tools/prover/mutants.py` plants single mutations in sqlglot code: and/or, comparison swaps, constants, removed `not`, deleted statements, comments, docstrings. `tools/prover/data_mutants.py` plants edits in fixture files and in shared test files. `tools/prover/bench.py` then runs every heavy test the prover called safe on the changed code. `tools/prover/lemma_checks.py` holds 17 hand-made edge cases. Results are in "Hardening" below.

**Hardening after the first submission.** The first submission missed one commit. The cause was in the file-read lemma: a file loaded in `setUpClass` (`tpc-ds.sql`, `tpc-h.sql`) was credited only to the first test of the class, so the executor tests were "proven" safe for TPC fixture edits. A review of every lemma then found and fixed:

| Bug | Fix |
| --- | --- |
| Files read in class setup credited to one test | Each heavy test is re-run alone and every line and file it touches is merged into the map |
| An edit to a test module that other test files import (`test_dialect.py`) was treated as affecting only that file | Any test file imported by another test file now affects every heavy test |
| An evaluation that raised partway through was not recorded, so "never evaluated" could be wrong | Such expressions are marked and never proven |
| Leaf values were encoded after the whole expression ran | Values are encoded when they are read |
| Reordered operands around a side-effecting call were not caught | The full evaluation order must match when any call is involved |
| Profiles came from one hash seed | Three seeds, merged |
| An empty patch, or one without `diff --git`, counted as "changes nothing" | Unprovable |
| Trees that differ with no located difference fell through to "docstring only" | Unprovable |
| `# coding:` comments were treated as harmless | Unprovable |
| Zero-context insertion hunks were applied one line early | Fixed |

Before the fixes, 5 of the 17 lemma checks failed. All pass now. With the new profiles: 109 code mutants (1,319 test runs) and 30 fixture and test-file mutants (591 test runs), with 0 tests failing after being called safe. Samples are unchanged at 124.9 s with 0 misses.

**Limits.** Profiles were recorded on macOS with Python 3.13, not inside the pinned image. A test that takes a different path in the scorer's environment could evaluate inputs we never recorded. The prover makes no claims about class-level constants (`WEEK_OFFSET = -2`). Real feature commits mostly add statements and fall back to the selector.

To rebuild the prover's data or check it against planted bugs, see `tools/prover/README.md`. `tools/prover/rebuild.sh` runs the whole pipeline.
