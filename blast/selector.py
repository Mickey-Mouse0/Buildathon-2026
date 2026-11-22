"""Blast Radius selector: for each patch, the tests that can see the change.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line a commit.
Logs go to stderr.
"""
import gc
import json
import os
import pickle
import subprocess
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
IMPORT_STATE = os.environ.get("BLAST_IMPORT_STATE", str(HERE / "importstate.pkl"))
KEYMAP = os.environ.get("BLAST_KEYMAP", str(HERE / "keymap.pkl.gz"))
TOKREPLAY = os.environ.get("BLAST_TOKREPLAY", str(HERE / "tokreplay.pkl.xz"))
TOKREPLAY_BASE = os.environ.get("BLAST_TOKREPLAY_BASE", str(HERE / "tokreplay_base.pkl"))
# Off by default: in the image it costs about 0.35 s of CPU a commit and saved less than
# that on every set we measured (samples, 30 full-suite commits, 309 planted bugs).
TOKREPLAY_ON = os.environ.get("BLAST_TOKREPLAY_ON", "0") == "1"
# Replay only when the tests it could drop are worth this many times its CPU.
REPLAY_MARGIN = float(os.environ.get("BLAST_REPLAY_MARGIN", "1.5"))
# Only pay for an import diff (~1 s of CPU) when its fallback would cost more than this.
# Off: the diff is also what notices a patch that breaks a module's import (a planted
# arg_types edit broke DuckDB's), where the fallback misses every test loading it. It
# skipped 4 of 360 planted bugs, about 0.3 s of CPU per 30 commits.
DIFF_MIN_GAIN_MS = int(os.environ.get("BLAST_DIFF_MIN_GAIN_MS", "0"))
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


def cost(numbers, ms):
    w = pack(numbers, ms)
    return max(sum(ms[n] for n in x) for x in w) + WORKER_MS * len(w) if w else 0


_baseline = []
_tests = []
_last_import = []
# Set by patched_import: the patched sqlglot failed to import (a module, or the child
# itself). The deferred fallback then isn't a safe answer: it only covers the setting
# reads, not the tests that load the broken module.
_import_broke = [False]
# The scorer stops us after 3 minutes of wall time. The child-process checks are
# optional, so they stop starting once a share of that is gone, and a child that
# runs long is cut off (its commit then keeps its tests).
_T0 = time.monotonic()
CHECK_DEADLINE_S = 110


def _time_left():
    return CHECK_DEADLINE_S - (time.monotonic() - _T0)


def _overlay(files, notes=None):
    """{path: patched source} for the patch's sqlglot .py files, or None."""
    overlay = {}
    for fc in files:
        p = fc.path
        if not (p.startswith("sqlglot/") and p.endswith(".py")):
            continue
        if fc.new_path is None:
            if notes is not None:
                notes.append(f"import diff: {p} deleted")
            return None
        old = ""
        if fc.old_path is not None:
            with open(os.path.join(SRC, fc.old_path)) as f:
                old = f.read()
        new = fc.apply(old)
        if new is None:
            return None
        overlay[fc.new_path] = new
    return overlay


def _load_baseline():
    if not _baseline:
        with open(IMPORT_STATE, "rb") as f:
            _baseline.append(pickle.load(f))
    return _baseline[0]


def _plain_setting(ck, name):
    base = _load_baseline()
    k = ("a", ck, name)
    e = base["snap"].get(k)
    return e is not None and e[1] == "plain" and k not in base.get("unstable", ()) and k not in base.get("alias", {})


def patched_import(files, notes, replay=None, budget=None, **replay_opts):
    """importstate.py's view of the patched sqlglot (and tokenizer replays), or None."""
    _load_baseline()
    overlay = _overlay(files, notes)
    if overlay is None:
        return None
    # the same hash seed as the build-time snapshot, so sets of strings line up
    if _time_left() < 5:
        notes.append("import diff skipped: out of time")
        return None
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    # the child fingerprints dict settings key by key only where they differ from these
    want = {k: _baseline[0]["snap"][k][0] for k in _baseline[0].get("dicts", ())}
    payload = {"files": overlay, "dicts": want, "replay": replay, "budget": budget, **replay_opts}
    _import_broke[:] = [False]
    try:
        r = subprocess.run([sys.executable, str(HERE / "importstate.py"), "patched", SRC],
                           input=pickle.dumps(payload), capture_output=True, env=env,
                           # an import takes about a second; a patch that makes it loop must not eat the run
                           timeout=min(150 if replay is not None else 20, _time_left()))
    except subprocess.TimeoutExpired:
        notes.append("import diff timed out")
        _import_broke[:] = [replay is None]
        return None
    if r.returncode != 0:
        notes.append(f"import diff failed: {r.stderr.decode(errors='replace')[-200:]}")
        _import_broke[:] = [True]
        return None
    after = pickle.loads(r.stdout)
    _last_import[:] = [overlay, after]
    _import_broke[:] = [bool(set(after["fail"]) - set(_baseline[0]["fail"]))]
    return after


def setting_changes(tok, maps, after, notes):
    """Settings the patch changes at import, and the tests that read them outside the
    tokenizer; None if anything else changes or a setting was never watched."""
    import tokreplay

    changed = tokreplay.changed_entries(_baseline[0], after)
    if changed is None:
        return None
    attrs = []
    for k in changed:
        b, a = _baseline[0]["snap"].get(k), after["snap"].get(k)
        if k[0] != "a" or b is None or a is None or b[1] != "plain" or a[1] != "plain":
            notes.append(f"replay: {k} changed at import")
            return None
        attrs.append(f"{k[1]}.{k[2]}")
        # subclasses that set it themselves (inherited reads are recorded on k[1])
        attrs += [f"{sc}.{k[2]}" for sc in maps.subclasses.get(k[1], ()) if ("a", sc, k[2]) in _baseline[0]["snap"]]
    if any(a in tok.collect for a in attrs):
        return None
    outside = tok.outside_readers(attrs)
    if outside is None:
        notes.append("replay: a changed setting was never watched")
    return outside


def import_diff(analyzer, files, notes):
    """Map test indexes for what the patch changes in sqlglot's state after import,
    or None if that can't be worked out."""
    after = patched_import(files, notes)
    return None if after is None else analyzer.diff_tests(_baseline[0], after, notes)


def tokenizer_replay(tok, maps, files, chosen, ms, notes):
    """chosen, minus tests whose every tokenizer call gives the same result with the
    patch, when the patch only changes code or settings used inside the tokenizer."""
    import tokreplay

    spots = tokreplay.changed_spots(files, SRC)
    if spots is None or not (spots[0] or spots[1]):
        return chosen
    if not tok:
        tok.append(tokreplay.TokReplay(TOKREPLAY, TOKREPLAY_BASE, {t["id"]: t["number"] for t in _tests[0]}))
    tok = tok[0]
    body, elsewhere = spots
    if tok.collect_calls is None or not tok.inside_only(body):
        return chosen
    need, keep = tok.needed(chosen)
    if elsewhere and _last_import and _last_import[0] == _overlay(files):
        # the import diff already ran on this patch: settle the settings part first
        outside = setting_changes(tok, maps, _last_import[1], notes)
        if outside is None:
            return chosen
        keep = keep | (outside & set(chosen))
    saving = cost(chosen, ms) - cost(keep, ms)
    est = tok.cost_ms(need) + 600
    if saving < REPLAY_MARGIN * est:
        notes.append(f"replay skipped: saves at most {saving / 1000:.1f}s for ~{est / 1000:.1f}s CPU")
        return chosen
    # a deterministic shuffle, so the probe at the front samples every kind of call
    need = sorted(need, key=lambda i: (i * 2654435761) % 4294967291)
    order = list(tok.collect_calls) + need
    after = patched_import(files, notes, replay=[tok.inputs[i] for i in order], budget=max(1.0, 2 * est / 1000),
                           replay_base=[tok.base[i] for i in order], probe=min(len(order), max(50, len(order) // 10)),
                           probe_max=0.2)
    if after is None:
        return chosen
    outside = setting_changes(tok, maps, after, notes)
    if outside is None:
        return chosen
    digests = after["replay"]
    ncol = len(tok.collect_calls)
    for j, i in enumerate(tok.collect_calls):
        if digests[j] is None or digests[j] != tok.base[i]:
            notes.append("replay: a call made while test modules load differs")
            return chosen
    done = {i: d for i, d in zip(need, digests[ncol:])}
    if need and all(d is None for d in digests[ncol:]):
        notes.append("replay: stopped early, the patch changes tokenizing broadly")
        return chosen
    affected = tok.affected(chosen, done)
    kept = [n for n in chosen if n in keep or n in outside or n in affected]
    notes.append(f"replay: {len(need)} calls, {len(affected)} tests differ, {len(outside & set(chosen))} read a changed "
                 f"setting outside, kept {len(kept)} of {len(chosen)}")
    return kept


TPC_MAP = os.environ.get("BLAST_TPC_MAP", str(HERE / "tpc_map.pkl.gz"))
TPC_EXPECTED = os.environ.get("BLAST_TPC_EXPECTED", str(HERE / "tpc_expected.pkl"))
# Per-query CPU estimates, pinned in the repo (tools/record_tpc_cpu.py) so every build's
# gate decides the same way. Measured at build they swung around the gate's cutoff.
TPC_CPU = os.environ.get("BLAST_TPC_CPU", str(HERE / "tpc_cpu.json"))
# Run the check only when its estimated CPU (+0.5 s) is under this share of the gain.
TPC_GATE = float(os.environ.get("BLAST_TPC_GATE", "0.95"))
# Off by default: on the samples and the practice set it only added CPU (checks that
# start and then keep the test); it paid off on planted commits only.
NO_CONSTANT_RULE = os.environ.get("BLAST_NO_CONSTANT_RULE") == "1"
NO_STAGED_TPC = os.environ.get("BLAST_STAGED_TPC", "1") != "1"
# The pinned per-query estimates against what the same queries cost under the scorer's
# limits (17 s for the ~14.8 s of a whole TPC-DS check on the practice board).
TPC_CPU_FACTOR = float(os.environ.get("BLAST_TPC_CPU_FACTOR", "1.2"))
# Starting the check: python, the patched import, loading the tables.
TPC_SETUP_S = float(os.environ.get("BLAST_TPC_SETUP_S", "2.5"))
_tpc = []


EXECUTOR_FILE = "tests/test_executor.py"

TPCDS_SQL = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCH_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpch"
# DuckDB, pandas and numpy, installed at build for blast/tpcquery.py only.
TPC_DEPS = os.environ.get("BLAST_TPC_DEPS", "/opt/tpcdeps")
# The check costs about 3 s of CPU plus the changed queries; ask for a clear margin.
TPC_QUERY_MIN_GAIN_MS = 8000


def tpc_query_edit(cid, files, chosen, ms, by_id):
    """Drop test_execute_tpcds when a patch only edits queries in tpc-ds.sql and the
    changed queries still pass the test's own comparison (blast/tpcquery.py)."""
    n = by_id.get(TPCDS_TEST)
    if n is None or n not in chosen or not os.path.isdir(TPC_DEPS):
        return chosen
    if len(files) != 1 or files[0].path != TPCDS_SQL or files[0].old_path != TPCDS_SQL or files[0].new_path != TPCDS_SQL:
        return chosen
    rest = [x for x in chosen if x != n]
    gain = cost(chosen, ms) - cost(rest, ms)
    if gain < TPC_QUERY_MIN_GAIN_MS or _time_left() < 45:
        return chosen
    with open(os.path.join(SRC, TPCDS_SQL), encoding="utf-8", newline="") as f:
        new = files[0].apply(f.read())
    if new is None or "\ufffd" in new:
        return chosen
    try:
        r = subprocess.run([sys.executable, str(HERE / "tpcquery.py"), SRC, TPC_DEPS], input=pickle.dumps({"text": new}),
                           capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=30)
        verdict = pickle.loads(r.stdout) if r.returncode == 0 else {"ok": False, "why": "check failed to run"}
    except subprocess.TimeoutExpired:
        verdict = {"ok": False, "why": "timed out"}
    log(cid, f"tpc query edit: {verdict['why']}; {'dropped test_execute_tpcds' if verdict['ok'] else 'kept'}")
    return rest if verdict["ok"] is True else chosen


def tpc_check(cid, analyzer, files, text, chosen, ms, by_id):
    """Drop a TPC executor test when the queries the patch reaches all give the
    pinned commit's results (blast/tpcmap.py). Queries it doesn't reach can't
    change, by the per-query map."""
    if not (os.path.exists(TPC_MAP) and os.path.exists(TPC_EXPECTED)):
        return chosen
    # The check replays the queries and data as they were at build time, so it can't
    # judge an edit to a test or fixture file (a TPC query file edit changes the query).
    # A file outside sqlglot/ only matters to a TPC test that opens it. The coverage
    # map lists who opens each data file (class setup included); a data file the map
    # doesn't know could matter to anyone. A test module matters only if
    # test_executor.py is it or imports it (conftest.py and __init__.py: always).
    m = analyzer.m
    others = sorted({p for fc in files for p in (fc.old_path, fc.new_path)
                     if p is not None and not p.startswith("sqlglot/")})

    def blocks(p):
        if not p.startswith("tests/"):
            return True
        if p.endswith(".py"):
            return (p == EXECUTOR_FILE or os.path.basename(p) in ("conftest.py", "__init__.py")
                    or EXECUTOR_FILE in analyzer._test_importers(p))
        return p not in m.open2t

    if any(blocks(p) for p in others):
        return chosen
    others = [p for p in others if not p.endswith(".py")]
    files = [fc for fc in files if fc.path.startswith("sqlglot/")]
    if not files:
        return chosen
    index_of = {t: i for i, t in enumerate(m.tests)}
    if not _tpc:
        import gzip

        with gzip.open(TPC_MAP, "rb") as f:
            qmaps = pickle.load(f)
        with open(TPC_EXPECTED, "rb") as f:
            exp = pickle.load(f)
        cpu = {t: [r["cpu"] for r in rows] for t, rows in exp.items()}
        front_cpu = {t: [r.get("front") for r in rows] for t, rows in exp.items()}
        if os.path.exists(TPC_CPU):
            with open(TPC_CPU) as f:
                pinned = json.load(f)
            for t in exp:
                p = pinned.get(t)
                if p is not None and len(p["cpu"]) == len(cpu[t]) == len(p["front"]):
                    cpu[t], front_cpu[t] = p["cpu"], p["front"]
        _tpc.append((qmaps, cpu, front_cpu))
    qmaps, cpu, front_cpu = _tpc[0]
    drop, jobs, est_front, est_full = set(), {}, 0.0, 0.0
    for test_id, qm in qmaps.items():
        # Never dropped by this check: tpc-h.sql has no executed queries, so the check
        # would only parse them, while the real test also builds and uses the tables.
        # Dropping it saved nothing in 6 of 7 cases where it did.
        if test_id == TPCH_TEST:
            continue
        n = by_id.get(test_id)
        if n is None or n not in chosen:
            continue
        if test_id not in index_of or any(index_of[test_id] in m.open2t[p] for p in others):
            continue
        def reached(qm):
            sub = Maps.__new__(Maps)
            sub.__dict__.update(m.__dict__)
            sub.__dict__.update({k: qm[k] for k in ("tests", "line2t", "open2t", "attr2t", "cls2t")})
            sub.tests_by_file = {}
            sub.all = frozenset(range(len(qm["tests"])))
            return Analyzer(sub).analyze(text, files)

        q, notes = reached(qm)
        if q is None:
            log(cid, f"tpc {test_id.rsplit('::', 1)[1]}: all queries ({'; '.join(notes[-2:])})")
            continue
        if not q:
            log(cid, f"tpc {test_id.rsplit('::', 1)[1]}: no query reaches the change")
            drop.add(n)
            continue
        # Queries the patch reaches only before Plan() (parse, optimize) are judged by
        # their optimized tree; the others run in full. Unknown means full.
        late = None
        if "exec" in qm and all(f is not None for f in front_cpu[test_id]) and not NO_STAGED_TPC:
            late, _ = reached(qm["exec"])
        full = set(q) if late is None else set(late)
        front = sorted(set(q) - full)
        jobs[test_id] = {"front": front, "full": sorted(full)}
        est_front += sum(front_cpu[test_id][i] for i in front)
        est_full += sum(cpu[test_id][i] for i in full)
    if jobs:
        rest = [x for x in chosen if x not in drop]
        gain = cost(rest, ms) - cost([x for x in rest if x not in {by_id[t] for t in jobs}], ms)
        # The check costs CPU whether or not it clears the tests. The front pass (each
        # query as far as Plan(), ~2 s for all of TPC-DS) runs when the queries that
        # must run in full anyway leave room under the gain. After it, the queries
        # whose tree changed must run in full too; the child gives up when those
        # would take more than what is left of the gain.
        est = TPC_SETUP_S + TPC_CPU_FACTOR * (est_front + est_full)
        allowed = TPC_GATE * gain / 1000
        if est >= allowed:
            log(cid, f"tpc check skipped: ~{est:.1f}s of queries for {gain / 1000:.1f}s")
        elif _time_left() < 3 * est + 10:
            log(cid, f"tpc check skipped: out of time ({_time_left():.0f}s left, ~{est:.1f}s of queries)")
        else:
            overlay = {}
            for fc in files:
                p = fc.path
                if fc.new_path is None or not p.endswith(".py"):
                    if p.startswith("sqlglot/"):
                        overlay = None
                        break
                    continue
                if not p.startswith("sqlglot/"):
                    continue
                old = ""
                if fc.old_path is not None:
                    with open(os.path.join(SRC, fc.old_path)) as f:
                        old = f.read()
                overlay[fc.new_path] = fc.apply(old)
                if overlay[fc.new_path] is None:
                    overlay = None
                    break
            if overlay is not None:
                # the hash seed the expected trees were recorded under
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
                budget = (allowed - TPC_SETUP_S - TPC_CPU_FACTOR * est_front) / TPC_CPU_FACTOR
                payload = {"overlay": overlay, "jobs": jobs, "cpu": {t: cpu[t] for t in jobs},
                           "front_cpu": {t: front_cpu[t] for t in jobs},
                           "full_budget": {t: budget for t in jobs},
                           # setup, with room for a slow machine; each query then has its own limit
                           "front_deadline": 3 * TPC_SETUP_S + 4}
                # A patch can make a query run for minutes. The check is cut off once it has
                # cost about what dropping the tests would save; the tests then stay.
                verdict, why = None, []
                limit = min(_time_left(), 3 * max(est, allowed) + 10)
                if RUNNER_ON:
                    # in a fork of the test server, where the tables are already loaded
                    try:
                        reply = _runner_call(dict(payload, kind="tpc", expected=TPC_EXPECTED, timeout=limit))
                        why.append(f"(cpu {reply['cpu'][0]:.2f}s server, {reply['cpu'][1]:.2f}s child)")
                        if reply["status"] == "ok":
                            verdict = reply["verdict"]
                        elif reply["status"] == "cut off":
                            verdict, why = {}, ["cut off"]
                        else:
                            why = [reply["status"]]
                    except Exception as e:
                        why = [repr(e)]
                if verdict is None:
                    try:
                        r = subprocess.run([sys.executable, str(HERE / "tpcmap.py"), "check", SRC, TPC_EXPECTED],
                                           input=pickle.dumps(payload), capture_output=True, env=env, timeout=limit)
                        verdict = pickle.loads(r.stdout) if r.returncode == 0 else {}
                        why += r.stderr.decode(errors="replace").strip().splitlines()[-1:] if r.stderr else []
                    except subprocess.TimeoutExpired:
                        verdict, why = {}, why + ["cut off"]
                stats = verdict.get("_stats", {})
                for test_id, qs in jobs.items():
                    ok = verdict.get(test_id, False)
                    st = stats.get(test_id, {})
                    log(cid, f"tpc {test_id.rsplit('::', 1)[1]}: {len(qs['front']) + len(qs['full'])} queries reached "
                             f"({len(qs['front'])} before planning only; {st.get('same_tree', 0)} same tree, {st.get('ran', 0)} run), "
                             f"{'same results, dropped' if ok else 'kept'} {'; '.join(why)}")
                    if ok:
                        drop.add(by_id[test_id])
    return [x for x in chosen if x not in drop]


RUNNER_ON = os.environ.get("BLAST_RUNNER", "1") == "1"
# Tests recorded at more than this are never run here: they stay selected.
RUN_MAX_MS = int(os.environ.get("BLAST_RUN_MAX_MS", "1200"))
# Run only when the estimated CPU is under this share of what dropping every
# runnable test would save.
RUN_SHARE = float(os.environ.get("BLAST_RUN_SHARE", "0.6"))
# Recorded seconds to CPU seconds here, for the estimate.
RUN_CPU_FACTOR = float(os.environ.get("BLAST_RUN_CPU_FACTOR", "0.9"))
# Starts its own python, which imports sqlglot from disk without the patch.
RUN_NEVER = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
_runner = []


def _runner_call(job):
    import select
    import struct

    if _runner and _runner[0] is None:
        raise RuntimeError("runner is off")
    if not _runner:
        # hash seed 0: the TPC check's expected trees were recorded under it
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
        _runner.append(subprocess.Popen([sys.executable, str(HERE / "runner.py"), SRC, TPC_DEPS],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env))
    p = _runner[0]
    data = pickle.dumps(job)
    p.stdin.write(struct.pack("<I", len(data)) + data)
    p.stdin.flush()
    # The server cuts its child off at job["timeout"]. If the server itself stops
    # answering (a test module that hangs on import), it is killed and stays off.
    fd = p.stdout.fileno()
    deadline = time.monotonic() + job["timeout"] + 20
    buf = b""
    while len(buf) < 4 or len(buf) < 4 + struct.unpack("<I", buf[:4])[0]:
        left = deadline - time.monotonic()
        chunk = os.read(fd, 1 << 16) if left > 0 and select.select([fd], [], [], left)[0] else b""
        if not chunk:
            p.kill()
            p.wait()
            _runner[0] = None
            raise RuntimeError("runner died or hung")
        buf += chunk
    return pickle.loads(buf[4:])


# Share of the possible saving that running the tests brings back on average (some
# of them fail and stay). Measured on the samples: 0.42.
RUN_YIELD = float(os.environ.get("BLAST_RUN_YIELD", "0.42"))
# One-time CPU, in ms: starting the server, importing a test module (pandas and
# DuckDB come with test_optimizer.py and test_executor.py), TestExecutor's setup.
RUN_SERVER_MS = 250
RUN_MODULE_MS = 40
RUN_PANDAS_MS = 600
RUN_EXECUTOR_MS = 2200
PANDAS_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}


def run_plan(cid, files, chosen, ms, tests):
    """What running this commit's light tests could save: None, or a dict."""
    for fc in files:
        p = fc.path
        if fc.old_path != fc.new_path or not (p.startswith("sqlglot/") and p.endswith(".py")):
            return None
    run = [n for n in chosen if ms[n] <= RUN_MAX_MS and tests[n]["id"] not in RUN_NEVER]
    if not run:
        return None
    runset = set(run)
    rest = [n for n in chosen if n not in runset]
    gain = cost(chosen, ms) - cost(rest, ms)
    est = RUN_CPU_FACTOR * sum(ms[n] for n in run) + 50
    net = RUN_YIELD * gain - est
    if net <= 0:
        log(cid, f"run skipped: ~{est / 1000:.1f}s for at most {gain / 1000:.1f}s")
        return None
    overlay = _overlay(files)
    if overlay is None:
        return None
    return {"cid": cid, "run": sorted(run), "est": est, "net": net, "overlay": overlay,
            "files": {tests[n]["file"] for n in run}}


def run_survivors(plans, chosen_of, ms, tests):
    """Drop, from each planned commit, the tests that pass when run on the patched
    code (blast/runner.py). Shared one-time costs must be covered by the commits
    that need them, or those commits are left as they are."""
    def needs(p):
        r = {"server"}
        if p["files"] & PANDAS_FILES:
            r.add("pandas")
        if "tests/test_executor.py" in p["files"]:
            r.add("executor")
        return r

    price = {"server": RUN_SERVER_MS, "pandas": RUN_PANDAS_MS, "executor": RUN_EXECUTOR_MS}
    plans = list(plans)
    for res in ("executor", "pandas", "server"):
        users = [p for p in plans if res in needs(p)]
        if users and sum(p["net"] for p in users) <= price[res]:
            for p in users:
                log(p["cid"], f"run skipped: {res} setup not worth it")
            plans = [p for p in plans if res not in needs(p)]
    for p in plans:
        cid = p["cid"]
        if _time_left() < 20:
            break
        try:
            timeout = min(_time_left() - 10, 3 * p["est"] / 1000 + 8)
            reply = _runner_call({"overlay": p["overlay"], "tests": [tests[n]["id"] for n in p["run"]], "timeout": timeout})
            res = reply["results"]
            passed = {n for n in p["run"] if res.get(tests[n]["id"]) is False}
            log(cid, f"run: {reply['status']}; {len(passed)} of {len(p['run'])} pass and are dropped "
                     f"(cpu {reply['cpu'][0]:.2f}s server, {reply['cpu'][1]:.2f}s child, est {p['est'] / 1000:.2f}s)")
            chosen_of[cid] = [n for n in chosen_of[cid] if n not in passed]
        except Exception as e:
            log(cid, "run error, keeping the tests:", repr(e))


def emit(cid, chosen, ms):
    workers = pack(chosen, ms)
    print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    return max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers) if workers else 0


def main():
    # The maps are millions of small objects; the cycle collector would rescan them
    # over and over while they load. Nothing here builds garbage cycles worth freeing.
    gc.disable()
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    _tests.append(tests)
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    every = sorted(ms)
    tok = None
    try:
        maps = Maps(MAP, SRC)
        to_number = [by_id.get(t) for t in maps.tests]
        keyreads = None
        if os.path.exists(IMPORT_STATE) and os.path.exists(KEYMAP):
            try:
                from keyreads import KeyReads

                keyreads = KeyReads(KEYMAP, {t: i for i, t in enumerate(maps.tests)})
            except Exception as e:
                log("key map failed to load, using attribute reads:", repr(e))
        analyzer = Analyzer(maps, import_diff=os.path.exists(IMPORT_STATE), keyreads=keyreads,
                            plain_setting=_plain_setting if os.path.exists(IMPORT_STATE) and not NO_CONSTANT_RULE else None)
        if TOKREPLAY_ON and os.path.exists(IMPORT_STATE) and os.path.exists(TOKREPLAY) and os.path.exists(TOKREPLAY_BASE):
            tok = []  # loaded the first time a patch could use it
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
    log(f"loaded in {time.process_time() - t0:.2f}s CPU")
    total = 0
    by_number = {t["number"]: t for t in tests}
    chosen_of, plans = {}, []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        chosen = every
        _c0 = os.times()
        _import_broke[:] = [False]
        # read before the analyzer's try: if it fails, the TPC check below must not
        # see the previous commit's patch
        text = patch.read_text(errors="replace")
        try:
            files = patchutil.parse(text)
            # a section with no ---/+++ lines (a pure rename or mode change, an empty or
            # binary file) parses to nothing and would otherwise select no tests for it
            if sum(1 for ln in text.splitlines() if ln.startswith("diff --git ")) != len(files):
                files = None
        except Exception:
            files = None
        try:
            if files is None:
                raise ValueError("patch does not parse")
            if analyzer is not None:
                idx, notes = analyzer.analyze(text, files)
                d = analyzer.deferred
                if idx is not None and d is not None:
                    num = lambda s: {to_number[i] for i in s if to_number[i] is not None}
                    base = num(idx)
                    if not d["all"] and cost(base | num(d["fallback"]), ms) - cost(base, ms) < DIFF_MIN_GAIN_MS:
                        idx = idx | d["fallback"]
                        notes.append("import diff skipped: fallback is cheap")
                    else:
                        _import_broke[:] = [False]
                        try:
                            extra = import_diff(analyzer, files, notes)
                        except Exception as e:
                            notes.append(f"import diff error {e!r}")
                            extra = None
                        if _import_broke[0]:
                            idx = None if extra is None else idx | extra
                            notes.append("import breaks: no fallback")
                        elif extra is not None and not d["all"] and \
                                cost(base | num(d["fallback"]), ms) < cost(base | num(extra), ms):
                            # both are safe answers; the diff can name more tests (a new
                            # class shows up in every registry it lands in)
                            idx = idx | d["fallback"]
                            notes.append("import diff: fallback is cheaper")
                        elif extra is not None:
                            idx = idx | extra
                        elif d["all"]:
                            idx = None
                        else:
                            idx = idx | d["fallback"]
                if idx is not None:
                    chosen = sorted({to_number[i] for i in idx if to_number[i] is not None})
                log(cid, "all" if idx is None else len(chosen), "; ".join(notes[:6] + notes[-4:] if len(notes) > 10 else notes))
        except Exception as e:
            log(cid, "error, running everything:", repr(e))
            chosen = every
        # A patch that breaks a module's import fails every test that loads it, in ways
        # the later checks (which reason about what the changed code computes) can't see.
        broke = _import_broke[0]
        if broke:
            log(cid, "import breaks: later checks skipped")
        if prover is not None and chosen and not broke and files is not None:
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
        if tok is not None and chosen and not broke and files is not None:
            try:
                tnotes = []
                chosen = tokenizer_replay(tok, maps, patchutil.parse(patch.read_text(errors="replace")), chosen, ms, tnotes)
                if tnotes:
                    log(cid, "; ".join(tnotes))
            except Exception as e:
                log(cid, "tokenizer replay error, keeping the tests:", repr(e))
        if chosen and files is not None and not broke:
            try:
                import fixturecheck

                fnotes = []
                safe = fixturecheck.judge(files, chosen, by_id, SRC, fnotes)
                if fnotes:
                    log(cid, "; ".join(fnotes))
                chosen = [n for n in chosen if n not in safe]
            except Exception as e:
                log(cid, "fixture check error, keeping the tests:", repr(e))
        if chosen and files is not None and not broke:
            try:
                import hlreplay

                n = by_id.get(hlreplay.TEST_ID)
                if n in chosen:
                    hnotes = []
                    mi = maps.tests.index(hlreplay.TEST_ID) if analyzer is not None and hlreplay.TEST_ID in maps.tests else None
                    opened = None if mi is None else (lambda p: mi in maps.open2t.get(p, ()))
                    if hlreplay.judge(files, SRC, hnotes, opened):
                        chosen = [x for x in chosen if x != n]
                        hnotes.append("dropped test_max_nodes")
                    if hnotes:
                        log(cid, "; ".join(hnotes))
            except Exception as e:
                log(cid, "max_nodes replay error, keeping the tests:", repr(e))
        if analyzer is not None and chosen and files is not None and not broke:
            try:
                chosen = tpc_check(cid, analyzer, files, text, chosen, ms, by_id)
            except Exception as e:
                log(cid, "tpc check error, keeping the tests:", repr(e))
        if chosen and files is not None and not broke:
            try:
                chosen = tpc_query_edit(cid, files, chosen, ms, by_id)
            except Exception as e:
                log(cid, "tpc query edit error, keeping the tests:", repr(e))
        chosen_of[cid] = chosen
        if RUNNER_ON and chosen and files is not None and not broke:
            try:
                plan = run_plan(cid, files, chosen, ms, by_number)
                if plan is not None:
                    plans.append(plan)
            except Exception as e:
                log(cid, "run plan error, keeping the tests:", repr(e))
        _c1 = os.times()
        log(cid, "cpu %.2fs self, %.2fs children" % (
            _c1.user + _c1.system - _c0.user - _c0.system,
            _c1.children_user + _c1.children_system - _c0.children_user - _c0.children_system))
        if not any(p["cid"] == cid for p in plans):
            total += emit(cid, chosen, ms)
    # Commits whose tests may be run are answered last: which runs are worth their
    # one-time costs depends on how many commits share them.
    if plans:
        try:
            run_survivors(plans, chosen_of, ms, by_number)
        except Exception as e:
            log("run stage error, keeping the tests:", repr(e))
        for p in plans:
            total += emit(p["cid"], chosen_of[p["cid"]], ms)
    if _runner and _runner[0] is not None:
        _runner[0].stdin.close()
        _runner[0].wait()
    t = os.times()
    log(f"estimated simulated {total / 1000:.1f}s, cpu {t.user + t.system:.2f}s here, "
        f"{t.children_user + t.children_system:.2f}s in children")


if __name__ == "__main__":
    main()
