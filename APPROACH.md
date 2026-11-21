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
| This selector | 170.2 s | 0 |
| Perfect answer | 94.4 s | 0 |
