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

## Edited TPC-DS queries

An edit to a query in `tpc-ds.sql` used to keep `test_execute_tpcds` (21.5 s), although most such edits still match DuckDB. The test handles each (meta, query) pair on its own, so pairs the edit leaves alone do what they did at the pinned commit. `blast/tpcquery.py` runs only the changed pairs, with the test module's own code: its `setUpClass`, the real loader on the patched file, `mp_execute` and `subtestHelper`. It costs about 3 s of CPU, and runs only for a patch that touches nothing but that file.

Passing in our container must also mean passing where the answers were labeled, so the changed query must have only one possible output on any machine. `order_is_fixed` requires a plain SELECT built from the kinds of expression the pinned executed queries use, no window, no inner LIMIT, and an ORDER BY over output columns under which no two different rows tie, including the first row a LIMIT cuts off (it asks DuckDB for one more row). Anything else keeps the test.

DuckDB, pandas and numpy are installed at build, at the locked versions, into `/opt/tpcdeps`, which only this check puts on its path. Nothing else in the image sees them.

Checked in the scorer's image: samples unchanged, and on 8 stand-in sets 2 commits change (25.5 s to 4.2 s each) with 0 misses. `tools/tpc_query_bench.py` planted 43 edits and ran the real test for each: 37 the check called ok all pass, 6 it kept all fail.

What it leans on: float sums that come out in a different order on another machine could still differ in their last bits. The unedited queries carry the same risk at the pinned commit.



## Staged TPC check

On by default since "TPC check: front pass first" below (`BLAST_STAGED_TPC=0` turns it off). It was off for a while: on the practice set it dropped nothing and added about 0.9 s of CPU, and it changes nothing on the samples; the gains below are on planted commits.

Each executed TPC query runs parse, optimize, plan, execute. Execution is about 80% of the cost (in the image: 10.8 s for all of TPC-DS, of which parse and optimize are 2.0 s), and most patches that reach a query only reach it before planning.

- `blast/tpcmap.py map` now also records, per query, what is reached from the call to `Plan()` until the result is back (`"exec"` in `tpc_map.pkl.gz`). Module-level caches are emptied at that point while recording, so anything a later stage would take from a cache is recomputed and counted as reached there.
- `expected` (image build) also stores a digest of each query's optimized tree (every slot of every node except the parent link and the cached hash), which tables `execute()` hands on, and the CPU up to that point.
- In `check`, a query the patch never reaches from `Plan()` on is a "front" query. If its tree digest and tables match the pinned commit's, planning and execution get the same input and run code the patch doesn't touch, so they are skipped. If they differ, the query runs in full as before. The DuckDB transpile is always run and compared.
- The selector's estimate uses the front CPU for those queries, so the cost gate passes far more often.

The check is also cut off once it has run for `max(2 x estimate + 3 s, 0.8 x gain)`; the tests then stay. One planted patch (`leave_tables_isolated=False` in `execute()`) made a query run for minutes, which used the whole time budget and left every later commit without its checks. The import check is cut off at 20 s for the same reason.

Factory rule: a function that runs at import and has a nested lambda was treated as a factory (only the tests that ran it). That misses a patch that makes it raise at import: every test file importing the module then fails to collect. Such patches now also go to the import diff.

Checked in the image (native arm64; the map was re-recorded under amd64 and is identical): samples 101.4 s and the 30-commit set 117.0 s, 0 misses on both. On 200 planted commits, 0 misses against tests run on the patched code; on the 8 commits that keep or check a TPC test, total CPU went from 61.8 s to 40.2 s.

Rebuild the map in the pinned image: `python blast/tpcmap.py map <sqlglot> blast/tpc_map.pkl.gz` (about 40 s).

## Two CPU cuts

- **Constant settings skip the import diff.** When a file's whole change is one class-level line `NAME = constant` whose constant changed, no class body copies the setting, no import-time code mentions it, and the pinned import leaves it as plain data, its value is all that differs after import. The readers are taken from the map directly (`_constant_swap` and `class_attr` in `blast/analyze.py`); `BLAST_NO_CONSTANT_RULE=1` sends these to the import diff again. Same output with and without on the samples, the 30-commit set and 200 planted commits; it fires on 1 sample and 3 planted commits and saves one child process each (about 0.4 s in the image). Checked on every class-level constant in sqlglot (1,022 patches, one per setting): the rule fires on 686 and gives the import-diff path's selection on 649. On 36 it selects fewer tests, and each extra test on the other path reads another class's own copy of the setting or a neighbouring setting on the next line, neither of which changed. On 1 it selects more (a setting the import overwrites).
- **The coverage map loads per file.** The image build rewrites `covmap.pkl.gz` uncompressed with one pickle per source file for the per-line table (`analyze.split_map`), and a file's table is unpickled the first time a patch touches it. Loading the map goes from 0.16 s to 0.08 s; output is identical to loading the whole map.

## TPC check: front pass first

The staged check is on again, and the check is ordered around it. It was switched off because it changed nothing on the practice set, but the final is a fresh draw of commits, and on stand-in commits most patches that reach `test_execute_tpcds` only reach it before planning.

- **Front pass.** Each query the patch reaches only before `Plan()` runs as far as `Plan()`, about a fifth of a full run. Same tree and same tables as the pinned commit: done. Heaviest first, so that queries that must run in full add up fast when they are going to be too many.
- **Probe.** As soon as one tree differs, the cheapest query that also differs runs in full. A patch that changes results usually changes many, so a real failure shows up after about one query instead of after the whole pass.
- **Budget.** The selector passes the per-query estimates and how much CPU the full runs may cost: 95% of the gain, less the setup (2 s) and the front pass. The child stops when the queries to run in full come to more. Estimates are scaled by 1.2, the practice board's 17 s for a 14.8 s check.
- The per-query CPU cut-off stays; a front query is held to its recorded front CPU.

Measured in the scorer's image under its limits (2 cores, 4 GB, read-only, user nobody). "Before" is main at 5ebe289 (runtime check included); simulated seconds + CPU seconds.

| Set | Before | After |
| --- | --- | --- |
| 30 samples | 99.0 + 6.7 | 99.0 + 7.6 |
| Stand-in set 1 (30 commits) | 157.7 + 8.1 | 132.5 + 18.4 |
| Set 2 (30) | 239.0 + 12.2 | 239.0 + 9.9 |
| Set 3 (30) | 104.6 + 6.6 | 104.6 + 6.8 |
| Set 4 (40) | 157.0 + 21.8 | 156.3 + 11.9 |
| Set 5 (40) | 155.8 + 48.3 | 174.6 + 23.7 |
| Set 6 (40) | 163.5 + 28.9 | 152.0 + 18.7 |
| Set 7 (40) | 143.1 + 7.4 | 143.1 + 7.6 |
| Set 8 (40) | 146.6 + 8.4 | 146.6 + 7.4 |
| All 290 stand-in commits | 1,409.1 total | 1,353.1 total |

The stand-in sets are single mutations from `tools/prover/make_eval_set.py`, one run each, so tenths are noise. Sets 3 and 7 and the samples come out 0.2 to 0.9 s higher (the front pass starting on commits where the test then stays); the other six are lower, set 6 by 21.7 s. On set 5 main selects less, because it ran one whole TPC-DS check that this version's budget turns down; it still spends 24.6 s less CPU there.

Checked: every test this version drops and main keeps was run under pytest on the patched tree, in the scorer's image: 4 commits, 3 of them dropping `test_execute_tpcds`, 0 failures. Samples: 0 misses.

## Runtime check: fresh process and skipped class setup

Two additions to the runtime check, both aimed at small selections of light tests that pass.

- **Fresh process.** The zygote can only swap function bodies, so a patch to a class or module body, to code that runs at import, or one that changes a function's code shape was never checked. For those, when the cost fits the same budget, `runcheck.py cold` starts a new process that imports the patched sqlglot through the overlay loader (every sqlglot module, as the zygote does with the pinned one), imports the selected tests' modules and runs them with the zygote's runner. It costs about 0.2 s of CPU in the image before the tests, or 1.4 s when a test module imports DuckDB and pandas. `BLAST_RUNCHECK_COLD=0` turns it off.
- **No class setup for `TestExecutor`.** Its `setUpClass` loads every TPC table, over a second of CPU, and only three tests read what it builds (`test_execute_tpch`, `test_execute_tpcds`, `test_optimized_tpch`), none of which the runtime check runs. When `tpc_setup_needed` says the patch cannot change that setup, the check replaces `setUpClass` and `tearDownClass` with nothing. A test that did read the tables would raise `AttributeError` and stay selected. The predicted cost drops with it, so more selections fit the budget. `BLAST_RUNCHECK_NOSETUP=0` turns it off.

Measured in the image (native arm64, the scorer's limits): the samples go from 98.97 s to 94.43 s with 0 misses, which is the cost of selecting exactly the failing tests. `5y72` now runs its 3 tests for 0.34 s of CPU instead of 1.19 s and drops them; `ktz3` (a class-level dict) runs in a fresh process for 0.31 s and drops its 3. On 200 planted commits, 5 change (6.7 s less), and no dropped test is one the labels say fails.

Not checked under `linux/amd64` on an Apple Silicon Mac: the recording step dies there with "Illegal instruction" inside the emulator, so the runtime check is off in that build and `make sample` gives 101.357 s with 0 misses.

## TPC check: trees that differ only in slots nothing reads

A front query used to count as unchanged only when its whole optimized tree matched the pinned one. Many optimizer patches change nothing but type annotations (`_type`) or node metadata (`_meta`): the SQL text, the plan, the generated Python and the rows are all the same, yet every query then had to run in full (about 20 s of CPU on the board) or the test stayed (21.5 s).

- **Which slots a query can read after planning.** `blind_slots` (image build) takes the lines each query reaches from `Plan()` on, from `tpc_map.pkl.gz`, and looks for any line that names `_type` or `_meta`, or that reaches attributes by a generic route (`getattr`, `__dict__`, `__slots__`, `deepcopy`, pickling). The `type` and `meta` properties and `is_type()` are themselves such lines in `expressions/core.py`, so a call to them is seen. `Expr.__hash__` and `__eq__` use the key and args only. The only hits are `Expr.__init__`, `copy`/`__deepcopy__` (which carry both slots into the copy and read nothing from them) and the generator's dispatch table; those lines are listed by their text in `_CARRY`. One query also reaches `is_type()` (`ArrayConcat` in the Python generator).
- **Result:** after planning, all 99 TPC-DS queries never read `_meta`, and 98 read neither slot.
- **The rule.** `expected` stores a second digest per query that leaves those slots out. In `check`, a front query whose full digest differs but whose relaxed digest and tables match is treated as unchanged: the run from `Plan()` on executes the same lines on the same values, apart from copies carrying different annotations. The check also copies the whole tree once, so a copy that would fail on the new annotations keeps the test. `BLAST_TPC_RELAXED=0` turns the rule off.

Measured in the image (native arm64): samples unchanged at 94.426 s. On 200 planted commits, three change (`annotate_types.py`, `qualify.py`, `normalize_identifiers.py`): `test_execute_tpcds` is dropped after the front pass alone, 40.3 s less in total. For each of the three, all 57 executed queries were run on the patched code and gave the pinned rows, and the real test was run alone under pytest: 57 subtests pass.

GC settings for the check were measured too (default, disabled, frozen after the tables load): 11.1 s against 10.4 to 10.9 s for a full TPC-DS pass, so they were left alone.

## TPC check: less fixed cost per pass

A CPU trace on 200 planted commits put the TPC check at 63% of all CPU (22 commits). A pass that takes all 57 executed queries as far as `Plan()` cost 3.1 s in the image, of which the optimizer is only 1.65 s. Two parts of the rest are gone:

- **Parsed fixture tables are cached at image build** (`tpcmap.py rows`, `tpc_rows.pkl`, 19 MB). The check always reads the pinned fixture files with this module's own parser, whatever the patch, so the rows are the same every time; only the `Table` built from them runs patched code, and it still does. Loading all tables goes from 0.48 s to 0.07 s.
- **The tree digest is 1.7 times faster** and feeds the hash the same bytes as before (compared on 712 trees, with and without the skipped slots).

Selections are identical on the samples and on the 200 planted commits; total CPU there went from 71.8 s to 65.8 s (native arm64). Tried and left out: replaying one optimizer rule from a saved tree (the rules are half the pass, and 56 of 57 saved trees came back different after pickling), and turning the collector off in the import-diff child (no change).
## Root maps: parse once, the one-line lemma, TypedDict bookkeeping

Three changes to the mapping and proving core. The first changes no output; the other two select fewer tests.

**Parse once.** The analyzer, the prover and the TPC check's per-query analysis each parsed the pinned file and the patched file again, and the prover compared the two by dumping both whole trees (`parser.py` is 9,000 lines). Now `blast/srccache.py` keeps the pinned tree for the run and the patched tree for the commit; the prover's `equal()` and `find_diffs` skip every statement whose source lines and columns are the same on both sides (the same tokens parse to the same tree) and descend only into the class or function that holds the change; a changed expression's place in the new tree is looked up once instead of once per heavy test; the runtime check's zygote keeps the pinned file's compiled code; and a patch that breaks an import the map can't place no longer reads every test file before answering "all". Output is identical on the samples and 180 stand-in commits.

**The one-line lemma** (`blast/linelemma.py`). A changed line used to select every test that ran any line of its statement, because an `and` to `or` flip on line 3 changes what line 1 does. A replaced line also marked the line after it, so the next statement's tests came along (or, below a method's last line, a class setting's readers and an import diff). The lemma replaces that with "the tests that ran this line" when the compiled code proves it is enough:

- the patch replaces exactly one line of the file, and every top-level statement sits where it did;
- the pinned and the patched statement are compiled and compared instruction by instruction: same count, same line for each, jump targets and exception handlers landing on the same instructions, same names and constants. Every instruction that differs must be on the changed line, in a function (not a class or module body, not a generator or coroutine) whose first instruction is on another line.

Then both versions run exactly alike until an instruction of that line executes, and the coverage map records a line whenever a frame enters it from another line. A test with no record of the line runs as it did at the pinned commit. The flip on line 3 is still caught: it changes jumps that belong to other lines, so the statement keeps all its tests. So are a constant folded into a tuple that starts on another line, an edit that adds or removes instructions (`is None` to `== None`), defaults, decorators and class bodies. The TPC check's per-query analysis uses the lemma too (its maps are recorded the same way). `BLAST_LINE_LEMMA=0` turns it off everywhere, `BLAST_TPC_LINE_LEMMA=0` only there.

What it leans on, and how each was checked:

| Assumption | Check |
| --- | --- |
| An instruction of line L never runs in a frame without a LINE event for L in that code object, except when the frame starts on L | `tools/line_event_check.py` runs sqlglot tests with LINE and INSTRUCTION events on: 98,032 executed instructions in 1,990 code objects, the only ones without a line record are on their function's first line (2,677), which the lemma refuses |
| The comparison accepts and refuses the right edits | `tools/line_lemma_checks.py`, 18 hand-made cases |
| No dropped test fails | `tools/line_lemma_bench.py` plants one-line edits where the lemma narrows the selection; every dropped test is run under pytest on the patched code: 200 edits (of 9,667 tried, the lemma held on 2,313), 8,550 dropped tests, none fails |

**TypedDict bookkeeping.** Flipping `total=False` on one of the five option TypedDicts in `sqlglot/_typing.py` changes only `__total__`, `__required_keys__` and `__optional_keys__` (on it and its subclasses). The import diff sent those to "every user of the class": 396 tests for `SchemaArgs`, every test for `_DialectArg`. A TypedDict is a plain dict when called and its subclasses' copies are entries of the same diff, so running code can only see the change by reading those names. `_typing_keys_unread` scans the pinned repository and the patch for them (and for `is_typeddict`, `__readonly_keys__`, `__mutable_keys__`); with no mention, such an entry selects nothing.

**A module's lazy loader selects every test (a miss fixed).** `sqlglot/dialects/__init__.py` and `sqlglot/optimizer/__init__.py` resolve names through a module-level `__getattr__` that stores what it finds in the module's globals, so it runs once per name in a process and the coverage map credits only the first test that asked. A stand-in commit that replaced the loader's first line with `pass` selected 1,126 tests while 22 others failed (`test_expressions`, `test_jsonpath`, `test_serde`, `test_tokens`, ...). A change inside a module-level `__getattr__`, or to a module global that one reads (its lookup table), now selects everything.

Measured against main under the scorer's limits on four stand-in sets of 45 commits (`tools/prover/make_eval_set.py`), selections checked against pytest with no miss from these rules: simulated 913.1 s to 829.1 s in total, which includes 8.4 s added by the lazy-loader rule. Two TypedDict flips account for 64 s of the gain, and the stand-in sets pick files uniformly, so a small file like `_typing.py` shows up more often there than it will in the final. The sample set is unchanged at 94.426 s. The parent process uses about 1.3 s less CPU per set.

## After a round of exploratory reviews

Three changes, each from a review of one stage, and one audit.

- **Slice recording could hide a dependency (fixed).** The runtime check runs only the fixture entries of a `tests/test_optimizer.py` test whose recorded run reaches changed code. The recorder ran all 167 tests in one process, so code that fills a per-process cache (`generator._build_dispatch` filling `_DISPATCH_CACHE`) was recorded for the first entry that needed it and for none after; a patch there would have run 7 of 13 sliced tests with no entries and passed them. `record_slices` now runs each test in its own fork of the warm process and restores sqlglot's module-level dicts, lists and sets before every entry, as the coverage recorder does before every test. `_build_dispatch` is now in the trace of 4,149 entries, up from 16. The change only adds entries to what a check runs. Not covered: caches held on objects that several entries share, for example a schema instance.
- **TPC check stops early when it is heading for "over budget".** After 6 front queries, if the CPU-weighted share whose tree differs, applied to the rest, puts the full runs over the budget, the check stops and the test stays. It can only keep a test. Three planted commits go from 2.0-2.7 s to 0.7-0.9 s of CPU each. `BLAST_TPC_EARLY=0` turns it off. The check also no longer parses each query a second time to learn which tables it needs; it takes the names the pinned run recorded, and a patched parse that wants another table fails in `run_query`, which keeps the test.
- **Runtime check predictor: 0.8 x recorded seconds, was 1.4.** Measured against real runs, the old figure was 2 to 4 times too high for tests that are not sliced. Two more planted commits now run their check and drop everything (19 and 27 tests, 1.1 s and 0.9 s of CPU, 10.5 s of simulated time). Samples: same output, 94.426 s, 0 misses. Total CPU could not be compared cleanly: the machine was under other load when this was measured.
- **Audit of every drop that is not a real run.** The planted labels turned out to miss tests that fail only through `subTest`. So for each of the 58 planted commits where a stage after coverage drops tests, every dropped test was run on the patched tree with the real process pools, counting subtest failures: 2,077 runs, all pass. (One patch, to `sqlglot/__main__.py`, drops the whole suite; run as one process, 22 `TestOptimizer` tests failed there, and the same 22 pass when run by themselves on that tree.)

Looked at and left out: `test_max_nodes` costs 26 s of CPU to run for real, so it cannot be checked by running it; a scaled-down input is not equivalent (integer identity, positions and the node limit all depend on size). A finer per-query stage map for the TPC check (skip the DuckDB transpile, the debug print or the parse when the patch never reaches them) measured 22% less per pass and is not built.

## TPC check: full runs continue from the front pass's tree

A front query whose tree differed used to be parsed and optimized a second time for its full run. The check now keeps the tree and tables the front pass reached `Plan()` with and finishes from there (`Plan`, `PythonExecutor.execute`, the pickle round trip), which is all `execute()` does after that point. Not used when the patch edits `sqlglot/executor/__init__.py`; `BLAST_TPC_REUSE=0` turns it off.

Measured in the image on two planted patches with the relaxed digest off so that the trees differ: same verdicts with and without (one passes all 57 queries, one fails), CPU 12.84 s to 10.92 s and 5.25 s to 4.80 s. Samples: same output as before, 94.426 s, 0 misses. `make sample` under amd64 and the larger planted run were not repeated for this change.

On the practice board, with the check switched off entirely (`BLAST_TPC_GATE=0`) simulated time rises by 41.5 s and CPU falls by 18.7 s; with the runtime check off, simulated rises by 7.4 s and CPU falls by 5.0 s. Both stay on.

## TPC check: a changed setting that is only read before planning

A class setting on the base `Dialect` or `Generator` is read on lines every TPC-DS query runs, so its per-query map called every query "reached after planning" and the check ran all of them in full (about 18 s on the board), or skipped as too dear and kept the test. Most such flips change a branch in the parser and leave every result as it was.

When the patch is a single constant swap of one setting and nothing else differs after import (`only_setting`), the constant lemma's per-read results are now used by stage. `Prover.constant_late_lines` returns the spans of the reads it cannot prove unchanged for the test; `tpc_check` keeps a query in the full-run set only if it runs one of those lines from `Plan()` on (the `exec` line map). The others can only differ before planning, which the front pass and its tree digest decide. `BLAST_TPC_SETTING_SITES=0` turns it off.

Checked on one patch per class-level constant in sqlglot (1,022): it changes the split on 4 (`ALIAS_POST_TABLESAMPLE`, `SUPPORTS_LIMIT_ALL`, `ALIAS_POST_VERSION`, `SET_OP_MODIFIERS`). All four are then dropped after the front pass with every tree unchanged, and for each a full run of all reached queries with the rule off gives the pinned rows. Samples: same output, 94.426 s, 0 misses. Not rerun: `make sample` under amd64 and the planted commits.

Measured on the way: of 254 one-line code edits on lines all queries reach after planning that the free stages cannot settle, 11 leave every result unchanged, with no common shape.

## TPC check: a lone `<` / `<=` or `>` / `>=` swap

A practice probe pointed at an edit on a lambda line in `sqlglot/executor/` for which the check ran most queries in full and found nothing changed. `"GTE": null_if_any(lambda this, e: this >= e)` with `>=` turned into `>` is such an edit: it only matters when the two sides are equal. The prover records values per expression but gives up past a number of distinct values, and a row-by-row comparison sees thousands.

`tpcmap.py cmp` (image build, `tpc_cmp.pkl`) imports sqlglot with every single `<`, `<=`, `>`, `>=` comparison rewritten into a call that evaluates both operators of the pair and passes the real result on, then runs every TPC-DS query the way the check does. For each comparison it keeps the queries in which the partner operator ever gave a different answer or raised (query -1: during import or table setup). When a patch is exactly one such comparison changed to its partner (`comparison_swap` in `blast/selector.py`: the two syntax trees are equal once that operator is put back), a query not listed computes on the patched code what it computed at the pinned commit, so only the listed queries are checked; with none listed the test is dropped. `BLAST_TPC_CMP_RULE=0` turns it off.

277 comparisons in 54 files; for 232 the partner never differs in any query. Checked on six executor patches against full runs of every reached query with the rule off: the same verdict on all six (two dropped, four kept), CPU for the six 24.8 s to 7.2 s. Samples: same output, 94.426 s, 0 misses; `make sample` 101.357 s, 0 misses.

## Comparison swaps, for every test

The two-operator recording now covers every test and two more swaps. `runcheck.py cmp` (image build, `runcheck_cmp.pkl`) imports sqlglot with every single `<`, `<=`, `>`, `>=`, `is` and `is not` comparison rewritten into a call that also works out what the operator a mutation would put there (`<` for `<=`, `>` for `>=`, `==` for `is`, `!=` for `is not`, and back) would have given, and passes the real result on. Each test then runs in its own fork of that warm process, so nothing an earlier test cached hides an evaluation. For `is` against `==` nothing extra is run unless it is sure to have no effect: the two are equal by construction for objects without their own `__eq__`, for a comparison with `None` on plain, container or sqlglot values, and plain values are compared directly; anything else counts as a disagreement. A comparison that disagrees during import is never used.

For a patch that is exactly one such comparison swapped (`comparison_swap`: one line replaced by one line, the syntax trees equal once the operator is put back), a recorded test in which the two operators never disagree runs the pinned run, so it is dropped before any other stage. Tests that were not recorded stay: `test_max_nodes`, the three TPC executor tests, and any test that failed or was not found while recording. `BLAST_CMP_TESTS_RULE=0` turns it off.

959 comparisons; 1,612 of 1,618 tests recorded in about 2 minutes of build. For 331 of 394 `is` and 243 of 288 `is not` comparisons the swap matters in no test. On 286 commits (85 comparison mutants on the lines every TPC-DS query runs, 200 planted commits, one sample) it changes 25 and drops 13,041 selected tests, several commits going from about 1,400 tests to 1. All 13,041 were run on their patched trees with the real process pools and subtest failures counted: all pass. Samples: same output, 94.426 s, 0 misses; it fires on `2haz`, `2qn5` and `66ks`, which the later stages already cleared.

Under amd64 emulation on an Apple Silicon Mac the recording step dies like the runtime check's does, so `make sample` there runs with the rule off: 101.357 s, 0 misses.
