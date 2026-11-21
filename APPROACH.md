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

## Blind spots fixed after the first practice miss

- **Per-process caches.** `generator._DISPATCH_CACHE` is filled once per process, so `_build_dispatch` was credited to whichever test ran first (47 tests; it really affects 1,347). The recorder now resets every module-level dict/list/set in sqlglot before each test. A planted `and`/`or` flip there was missed by the earlier versions and is caught now.
- **Code run while sqlglot is imported.** Metaclasses, `__init_subclass__` and helpers called from class bodies run before any test. A change in such a function body now runs everything, unless the function is a factory whose output lives on in the functions it returns; then the tests that ran those.
- **Constants read directly by tests.** Module constants are looked up in `tests/` too (`test_typing` reads `EXPRESSION_METADATA`).
- The prover's "never executed" lemma now reads this map, which has the same fixes and was recorded in the scorer's base image.
- The cycle collector is off while the maps load: CPU on the samples went from 3.9 s to 1.3 s.

Checked: samples 124.9 s, 0 misses. Planted-bug and real-commit checks are being re-labeled (the first labels missed SUBFAILED subtests).

## All-tests replay

The value-replay lemma now also runs over every test, not just the 31 heavy ones. Every one of the 1,620 tests was recorded under 3 hash seeds (`test_lazy_load` excepted, because it starts its own `python`), each in a fork of one post-import process. The records of all tests are merged per expression into `blast/prover_data/union.pkl.xz` (0.9 MB). The change must also be an operator expression, and replay must give the same value as before on every recorded evaluation from any test (or the expression was never evaluated). When both hold, the whole commit cannot change any profiled test, and only unprofiled tests stay selected. On the samples, `2haz` and `66ks` now cost 0: 124.9 s becomes 118.6 s with 0 misses, at about +0.5 s CPU. Every whole-commit claim on the planted bugs was checked by running every test that covers the changed file.

## Import diff

Some changes take effect while sqlglot is being imported: a setting that a metaclass or `__init_subclass__` reads to build another one (`NUMERIC_ESCAPES` → `ESCAPED_SEQUENCES`, `TIME_MAPPING` → `TIME_TRIE`), the body of that import-time code, a class header or decorator, an import line. Per-test coverage can't say who depends on what was built, so these used to fall back to every user of the class, or to every test.

Now the selector imports the patched sqlglot in a child process (`blast/importstate.py`, patched files served from memory) and fingerprints every class attribute and module global, including classes built at import that no module names. It compares that with the same fingerprint of the pinned commit, taken at build time (`tools/build_importstate.sh`, under hash seed 0, with entries that vary between processes marked). Then it maps what differs:

- a setting: the tests that read it (on that class, its subclasses, and on bases if it starts or stops shadowing one);
- a module constant: the module-constant rule;
- a function or method defined right there with `def`: nothing extra, since the line rules cover its body and signature;
- anything else (a method added or replaced, a dunder such as a dataclass's `__hash__`, a class built at import): every user of the class, meaning tests that read its settings or ran a function that names it;
- an enum member, or a module that now fails to import: every test.

Each diff costs about 0.6 s of CPU, so it runs only when its fallback would cost more than 1.2 s. sqlglot is also pre-compiled in the image, which saves about 0.4 s of CPU per run.

The recorder also had a gap: it switched on its import bucket only after `import sqlglot`, so function bodies run during that import were recorded nowhere. It is fixed, and `tools/import_lines.py` added the missing 176 lines to the shipped map.

Checked (corrected labels, 0 misses everywhere): samples 118.6 → 115.0 s, planted bugs 334.2 → 244.5 s, real commits 565.0 → 509.1 s.

## Key-level reads of dict settings

Most class settings are dicts (`Parser.FUNCTIONS`, `Tokenizer.KEYWORDS`, `NUMERIC_ESCAPES`, ...). The attribute map says a test read the dict, which is true of nearly every test that builds a parser or tokenizer of that class, even though most of them never look at the entry a patch changes. `4d63` (one value in Snowflake's `NUMERIC_ESCAPES`) selected 273 tests; none of them looks up that key.

`tools/keymap_plugin.py` swaps every class-level dict (1,759 of them) for an equal dict that records, per test, every key looked up through it, misses included. Iteration, `len` and `bool` count as reading the key list; `items`, `values`, `==`, copies, `|`, pickling and `deepcopy` count as reading everything. `{**d}`, `dict(d)` and `**d` read each key one by one, so they are recorded per key. The import diff now fingerprints each dict key by key (`importstate.dict_info`), so a patch to a dict setting, or to anything derived from one at import, selects only the tests that read a changed key, the key list when the order changes, or everything.

Why reading other keys is safe: a test's run can only differ from the pinned commit's at a read that returns something different. Until then it is the same run, so it reads exactly the keys we recorded, including keys computed from other entries (`D[D["a"]]` records `"a"`). What that argument needs, and how it is covered:

| Way dict state could reach a test unseen | Covered by |
|---|---|
| A lookup that missed now hits | Misses are recorded |
| A warm cache filled by an earlier test | Each test runs in a fork of the post-import parent |
| Another reference to the same dict | Dicts referenced from anywhere but their own classes (20) are not wrapped and keep attribute-level reads; a change in which settings share one dict counts as a change |
| `True == 1 == 1.0`, enum and class keys | Keys are matched by Python equality (`importstate.norm_key`); keys we can't reason about match everything |
| Seed-dependent lookup order | Recorded under hash seeds 0, 1 and 2 and merged |
| Reads in another process | A test that starts one (`test_lazy_load`) is left unrecorded and keeps attribute-level reads |
| Reads while test modules import | If they touch a changed key, nothing is narrowed |
| A test that fails under the recorder | Left unrecorded |

Every edited class setting now goes to the import diff when it is available. Before, a one-line replacement also counted the untouched setting on the next line (the `+` side looked like an insertion in front of it), and `4d63` kept `DROP_UNKNOWN_ESCAPES`'s 273 readers. Settings whose values can't be fingerprinted keep their readers as before.

Checked: `tools/keymap_checks.py` (24 toy tests reaching a dict in different ways, 12 kinds of edit, 0 misses); `tools/keymap_bench.py` (80 planted edits to real dict settings, 16,228 dropped tests run on the patched code, 0 failures; selection over those edits 32,344 → 17,431 tests). Samples 115.0 → 103.9 s, 0 misses. Rebuild: `python tools/build_keymap.py <sqlglot> sample/tests.json blast/keymap.pkl.gz` (about 12 minutes on 12 cores).

## Tokenizer replay

A patch to tokenizer code (`tokenizer_core.py` holds most of it) is seen by almost every test, so it used to select almost everything. But the tokenizer is a clean boundary: tokenizing resets all per-call state, so each call is a function of the tokenizer class, the dialect's fields and the SQL. If every call a test makes gives the same tokens, error, tokenizer state, log records and warnings with the patched code, the test runs exactly as before.

`tools/tokreplay_plugin.py` records, per test (3 seeds, one fork per test):
- every tokenize call as (tokenizer class, dialect fields, sql). Right after each call it tokenizes again with a fresh tokenizer built from that description; if the result differs, the test is marked not replayable (8 Athena tests are);
- which function code ever starts outside a tokenizer call, as line ranges. In the tokenizer files that is only `Token`'s helpers and the `tokens` property, so a patch to any other line there only ever runs inside;
- every class setting read outside a tokenizer call.

At selection time (`blast/tokreplay.py`, `tokenizer_replay` in the selector), a patch qualifies when every changed function-body line only ever runs inside the tokenizer, and the import diff shows nothing changed but plain settings. The import-time runs of the tokenizer (sqlglot parses SQL while it imports) are covered by that clean diff. The patched sqlglot is imported in the import-diff child, which replays the calls of the selected tests and compares them with digests made from the pinned commit at image build time. Kept: tests whose calls differ, tests that read a changed setting outside the tokenizer, and tests without a usable recording.

All 24,768 distinct calls replay in about 16 s of CPU, so replay runs only when the tests it could drop are worth 1.5 times its estimated CPU. It replays a shuffled 10% first and stops if more than 20% of those differ, since such a patch changes tokenizing broadly. Rebuild: `python tools/build_tokreplay.py <sqlglot> sample/tests.json blast/tokreplay.pkl.xz` (about 25 minutes).

## Prover profiles split by file

The heavy-test profiles (28 MB unpickled) used to load whole at startup, which also imports much of sqlglot: about 0.7 s of CPU on every run. The image build now regroups them by sqlglot file (`blast/prover/shard.py`), and the prover loads a file's shard the first time a patch changes an expression in it. Nothing is re-recorded: every record, overflow and raised entry is the same, only grouped by the file its expression is in. Startup CPU goes from about 1.2 s to about 0.4 s, with identical output on the samples.

## TPC fixture rules

Two cases where `test_execute_tpcds` (21.5 s) or `test_execute_tpch` was kept for nothing:

- **An edit to the expected side of a TPC query file.** The execute tests read only each pair's meta and query; `test_optimized_tpch` is the one that reads the expected side. When the patched file gives the same (meta, query) pairs in the same order, the execute tests are dropped (`tpc_sql` in `blast/analyze.py`).
- **A patch that also edits an unrelated data file.** The TPC check used to stand down whenever a patch touched anything outside `sqlglot/`. It now stands down only for a test file, a data file the coverage map doesn't know, or a data file the TPC test opens (class setup included).

Checked in the scorer's image on 8 stand-in sets (240 commits, labeled by running the selected tests): 3 commits change, 0 misses, and the three dropped tests pass when run on the patched code.



## Staged TPC check

Each executed TPC query runs parse, optimize, plan, execute. Execution is about 80% of the cost (in the image: 10.8 s for all of TPC-DS, of which parse and optimize are 2.0 s), and most patches that reach a query only reach it before planning.

- `blast/tpcmap.py map` now also records, per query, what is reached from the call to `Plan()` until the result is back (`"exec"` in `tpc_map.pkl.gz`). Module-level caches are emptied at that point while recording, so anything a later stage would take from a cache is recomputed and counted as reached there.
- `expected` (image build) also stores a digest of each query's optimized tree (every slot of every node except the parent link and the cached hash), which tables `execute()` hands on, and the CPU up to that point.
- In `check`, a query the patch never reaches from `Plan()` on is a "front" query. If its tree digest and tables match the pinned commit's, planning and execution get the same input and run code the patch doesn't touch, so they are skipped. If they differ, the query runs in full as before. The DuckDB transpile is always run and compared.
- The selector's estimate uses the front CPU for those queries, so the cost gate passes far more often.

The check is also cut off once it has run for `max(2 x estimate + 3 s, 0.8 x gain)`; the tests then stay. One planted patch (`leave_tables_isolated=False` in `execute()`) made a query run for minutes, which used the whole time budget and left every later commit without its checks. The import check is cut off at 20 s for the same reason.

Factory rule: a function that runs at import and has a nested lambda was treated as a factory (only the tests that ran it). That misses a patch that makes it raise at import: every test file importing the module then fails to collect. Such patches now also go to the import diff.

Checked in the image (native arm64; the map was re-recorded under amd64 and is identical): samples 101.4 s and the 30-commit set 117.0 s, 0 misses on both. On 200 planted commits, 0 misses against tests run on the patched code; on the 8 commits that keep or check a TPC test, total CPU went from 61.8 s to 40.2 s.

Rebuild the map in the pinned image: `python blast/tpcmap.py map <sqlglot> blast/tpc_map.pkl.gz` (about 40 s).
