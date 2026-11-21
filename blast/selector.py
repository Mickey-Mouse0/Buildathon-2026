"""Blast Radius selector: for each patch, the tests that can see the change.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line a commit.
Logs go to stderr.
"""
import gc
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import patchutil  # noqa: E402
from analyze import Analyzer, Maps  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SRC = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", str(HERE / "covmap.pkl.gz"))
PROVER_DATA = Path(os.environ.get("BLAST_PROVER_DATA", str(HERE / "prover_data")))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


def pack(numbers, ms):
    """Cheapest split: try 1..8 workers, longest test first onto the lightest worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    best, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            workers[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return best


class _LineView:
    """{str(line): test numbers} for one file: the prover's own map plus the selector's."""

    def __init__(self, per_line, to_number, own):
        self.per_line = per_line or {}
        self.to_number = to_number
        self.own = own or {}

    def get(self, key, default=()):
        s = self.per_line.get(int(key))
        mine = {self.to_number[i] for i in s} if s else set()
        theirs = self.own.get(key, ())
        if not mine and not theirs:
            return default
        return mine | set(theirs)


class _FileView:
    def __init__(self, line2t, to_number, own):
        self.line2t = line2t
        self.to_number = to_number
        self.own = own

    def get(self, path, default=None):
        return _LineView(self.line2t.get(path), self.to_number, self.own.get(path))


def use_selector_coverage(prover, maps, to_number):
    """Let the prover's "never executed" lemma see the selector's map as well.

    That map was recorded inside the scorer's base image and resets per-process caches
    between tests, so code that fills a cache is credited to every test relying on it.
    A line counts as executed by a test if either map says so.
    """
    prover.lines = _FileView(maps.line2t, to_number, prover.lines)
    imported = {f: set(v) for f, v in prover.import_lines.items()}
    for f, v in maps.importtime.items():
        imported.setdefault(f, set()).update(v)
    prover.import_lines = imported


def main():
    # The maps are millions of small objects; the cycle collector would rescan them
    # over and over while they load. Nothing here builds garbage cycles worth freeing.
    gc.disable()
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    every = sorted(ms)
    try:
        maps = Maps(MAP, SRC)
        to_number = [by_id.get(t) for t in maps.tests]
        analyzer = Analyzer(maps)
    except Exception as e:
        log("map failed to load, running everything:", repr(e))
        analyzer = None
    prover = None
    try:
        sys.path.insert(0, str(HERE / "prover"))
        from prove import load_packaged

        prover = load_packaged(PROVER_DATA, SRC, tests)
        if analyzer is not None:
            use_selector_coverage(prover, maps, to_number)
        log(f"prover loaded for {len(prover.heavy)} heavy tests")
    except Exception as e:
        log("prover failed to load, using the selector alone:", repr(e))
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        chosen = every
        try:
            if analyzer is not None:
                text = patch.read_text(errors="replace")
                idx, notes = analyzer.analyze(text, patchutil.parse(text))
                if idx is not None:
                    chosen = sorted({to_number[i] for i in idx if to_number[i] is not None})
                log(cid, "all" if idx is None else len(chosen), "; ".join(notes[:8]))
        except Exception as e:
            log(cid, "error, running everything:", repr(e))
            chosen = every
        if prover is not None and chosen:
            try:
                patch_text = patch.read_text(errors="replace")
                all_safe, why = prover.union_verdict(patch_text)
                verdict = None if all_safe else prover.verdict(patch_text)
                if all_safe:
                    kept = [n for n in chosen if n not in prover.union_profiled]
                    log(cid, f"prover: every profiled test unaffected ({why}); keeping {len(kept)} unprofiled")
                    chosen = kept
                elif verdict["noop"]:
                    log(cid, "prover: commit changes nothing")
                    chosen = []
                else:
                    dropped = [n for n in chosen if n in verdict["safe"]]
                    chosen = [n for n in chosen if n not in verdict["safe"]]
                    if dropped:
                        log(cid, "prover dropped", dropped)
            except Exception as e:
                log(cid, "prover error, keeping the selector's tests:", repr(e))
        workers = pack(chosen, ms)
        if workers:
            total += max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    log(f"estimated simulated {total / 1000:.1f}s, cpu so far {time.process_time() - t0:.2f}s")


if __name__ == "__main__":
    main()
