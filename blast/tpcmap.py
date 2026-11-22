"""Per-query coverage for the TPC executor tests, and their per-query outputs.

    python blast/tpcmap.py map <sqlglot dir> <out tpc_map.pkl.gz>          (offline, committed)
    python blast/tpcmap.py expected <sqlglot dir> <out tpc_expected.pkl>  (image build)
    python blast/tpcmap.py check <sqlglot dir> <expected.pkl>  < pickle {"overlay", "jobs"}

test_execute_tpcds (21.5 s) and test_execute_tpch (3.9 s) run every query of a
fixture file through parse_one, a pickle round trip (the test uses a process
pool), execute(), and compare against DuckDB running transpile(sql, "duckdb").
They see almost any optimizer change, so the per-test map picks them a lot.

"map" records, per query, the lines, settings and files it touches, in the same
shape as covmap.pkl.gz, so blast/analyze.py can tell which queries a patch
reaches. Work shared by all queries (loading the tables) counts for every one.

"expected" records what the pinned commit computes per query: the result table
and the DuckDB SQL. It runs at image build, so it matches the scorer's runtime.
"""
import ast
import csv
import gzip
import hashlib
import os
import pickle
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")

SUITES = {
    # test id: (fixture dir under tests/fixtures/optimizer, sql file, schema name)
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpc-ds", "tpc-ds.sql", "TPCDS_SCHEMA"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpc-h", "tpc-h.sql", "TPCH_SCHEMA"),
}


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


def load_tables(src, fixture, schema, names=None):
    """Same parsing as TestExecutor.setUpClass, for the tables named (or all)."""
    from sqlglot.executor.table import Table

    tables = {}
    for table, columns in schema.items():
        if names is not None and table not in names:
            continue
        reader = csv.reader(open_file(f"{src}/tests/fixtures/optimizer/{fixture}/{table}.csv.gz"), delimiter="|")
        rows, ctypes = [], []
        next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)
    return tables


def suite(src, test_id):
    from tests import helpers

    fixture, sql_file, schema_name = SUITES[test_id]
    return fixture, list(helpers.load_sql_fixture_pairs(f"optimizer/{fixture}/{sql_file}")), getattr(helpers, schema_name)


class _SameTree(BaseException):
    """Raised at Plan() to skip planning and execution. Not an Exception, so nothing
    in sqlglot can swallow it."""


class _OtherTree(BaseException):
    """Raised at Plan() by a front pass that found a different tree."""


SAME = object()
OTHER = object()
_hook = {"at_plan": None, "tabs": None, "needed": None, "on": False}


def install_hook():
    if _hook["on"]:
        return
    import sqlglot.executor as ex

    plan, ensure = ex.Plan, ex.ensure_tables

    def ensure_tables(*a, **k):
        _hook["tabs"] = r = ensure(*a, **k)
        return r

    def Plan(expression, *a, **k):
        if _hook["at_plan"] is not None:
            _hook["at_plan"](expression, _hook["tabs"])
        return plan(expression, *a, **k)

    ex.Plan, ex.ensure_tables = Plan, ensure_tables
    _hook["on"] = True


def tree_digest(root):
    """The parent link and the cached hash are derived from the rest, so they are left
    out; every other slot of every node counts, because planning or execution may read it."""
    from sqlglot import exp

    h = hashlib.blake2b(digest_size=16)
    put = h.update
    slots_of = {}

    def slots(cls):
        r = slots_of.get(cls)
        if r is None:
            # subclasses repeat the base's slot names: visit each name once
            names = dict.fromkeys(s for c in cls.__mro__ for s in getattr(c, "__slots__", None) or ())
            r = slots_of[cls] = [s for s in names if s not in ("parent", "_hash", "__dict__", "__weakref__")]
        return r

    def walk(v):
        if isinstance(v, exp.Expr):
            cls = type(v)
            put(f"({cls.__module__}.{cls.__qualname__}".encode())
            for s in slots(cls):
                put(b"|" + s.encode() + b"=")
                walk(getattr(v, s, None))
            d = getattr(v, "__dict__", None)
            if d:
                walk(d)
            put(b")")
        elif isinstance(v, dict):
            put(b"{")
            for k, x in v.items():
                put(repr(k).encode() + b":")
                walk(x)
            put(b"}")
        elif isinstance(v, (list, tuple)):
            put(b"[" + type(v).__name__.encode())
            for x in v:
                walk(x)
                put(b",")
            put(b"]")
        elif isinstance(v, (set, frozenset)):
            put(b"<" + repr(sorted(map(repr, v))).encode() + b">")
        else:
            put(type(v).__name__.encode() + b":" + repr(v).encode())

    walk(root)
    return h.hexdigest()


def tabs_shape(tabs, needed):
    """What execute() hands the executor besides the tree: which of the caller's tables
    sits under which name, and that their size and columns are as they were loaded."""
    names = {id(t): n for n, t in needed.items()}
    mapping = getattr(tabs, "mapping", None)
    if not isinstance(mapping, dict):
        return None
    return (
        tuple((k, names.get(id(v)), len(getattr(v, "rows", ())), tuple(getattr(v, "columns", ())))
              for k, v in mapping.items()),
        repr(getattr(tabs, "supported_table_args", None)),
    )


def run_query(meta, sql, schema, tables, at_plan=None, at_post=None):
    """What the test computes for one query: (result columns and rows or None, duckdb sql).
    at_plan(tree, tables) runs right before planning; if it raises _SameTree the
    result comes back as SAME. at_post() runs before the DuckDB transpile."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute

    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return None, None  # the test only parses and pickles it
    needed = {t.name: tables[t.name] for t in find_tables(expression)}
    _hook["needed"], _hook["at_plan"] = needed, at_plan
    try:
        table = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=needed)))
        res = (tuple(table.columns), [tuple(r) for r in table.rows])
    except _SameTree:
        res = SAME
    except _OtherTree:
        res = OTHER
    finally:
        _hook["at_plan"] = None
    if at_post is not None:
        at_post()
    return res, transpile(sql, write="duckdb")[0]


def build_map(src, out):
    os.environ.setdefault("COVMAP_OUT", "/tmp/unused")
    os.environ["COVMAP_ROOT"] = src
    sys.path.insert(0, TOOLS)
    import covmap_plugin as P

    mon = P.mon
    P.start()
    install_hook()

    def switch(bucket):
        # module-level caches are emptied too, so whatever a later stage takes from
        # a cache is recomputed and recorded as reached in that stage
        P.reset_globals()
        P.state["cur"], P.state["opened"], P.state["attrs"] = bucket
        mon.restart_events()

    result = {}
    for test_id in SUITES:
        common = [set(), set(), set()]
        P.state["cur"], P.state["opened"], P.state["attrs"] = common
        mon.restart_events()
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        pickle.loads(pickle.dumps((schema, tables)))  # the pool's initargs
        per_q, per_q_exec = [], []
        for meta, sql, _ in sqls:
            front, late, post = ([set(), set(), set()] for _ in range(3))
            switch(front)
            try:
                run_query(meta, sql, schema, tables, lambda tree, tabs: switch(late), lambda: switch(post))
            except Exception as e:  # it passes at the pinned commit; just in case
                print("query failed", test_id, len(per_q), repr(e), file=sys.stderr)
            P.state["cur"], P.state["opened"], P.state["attrs"] = None, None, None
            per_q.append([front[i] | late[i] | post[i] | common[i] for i in range(3)])
            per_q_exec.append([late[i] | common[i] for i in range(3)])

        def tables_of(buckets):
            line2t = defaultdict(lambda: defaultdict(set))
            open2t, attr2t, cls2t = defaultdict(set), defaultdict(set), defaultdict(set)
            for i, (lines, opened, attrs) in enumerate(buckets):
                for f, l in lines:
                    line2t[f][l].add(i)
                for f in opened:
                    open2t[f].add(i)
                for k in attrs:
                    attr2t[k].add(i)
                    cls2t[k.rsplit(".", 1)[0]].add(i)
            return {
                "tests": [f"q{i + 1}" for i in range(len(buckets))],
                "line2t": {f: {l: frozenset(s) for l, s in m.items()} for f, m in line2t.items()},
                "open2t": {f: frozenset(s) for f, s in open2t.items()},
                "attr2t": {k: frozenset(s) for k, s in attr2t.items()},
                "cls2t": {k: frozenset(s) for k, s in cls2t.items()},
            }

        result[test_id] = tables_of(per_q)
        result[test_id]["executes"] = [bool(meta.get("execute")) for meta, _, _ in sqls]
        result[test_id]["exec"] = tables_of(per_q_exec)
        print(f"{test_id}: {len(per_q)} queries", file=sys.stderr)
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def build_expected(src, out):
    import time

    sys.setrecursionlimit(20000)
    install_hook()
    result = {}
    for test_id in SUITES:
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        rows = []
        for meta, sql, _ in sqls:
            seen = {}

            def at_plan(tree, tabs):
                seen["front"] = time.process_time() - t
                seen["tree"], seen["tabs"] = tree_digest(tree), tabs_shape(tabs, _hook["needed"])

            def at_post():
                seen["post"] = time.process_time()

            t = time.process_time()
            res, duck = run_query(meta, sql, schema, tables, at_plan, at_post)
            end = time.process_time()
            rows.append({"result": res, "duckdb": duck, "cpu": end - t, "tree": seen.get("tree"), "tabs": seen.get("tabs"),
                         "front": seen["front"] + end - seen["post"] if "front" in seen else end - t})
        result[test_id] = rows
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def class_setup(src):
    """What test_execute_tpcds's module and class setup do with sqlglot, on the patched
    code: the module's sqlglot imports, every table of both schemas built the way
    setUpClass builds them (it builds them all, TPC-H included, before any test
    runs), and the pickle round trip of all the TPC-DS tables that the test's process
    pool makes. Raises where the real test would error. Returns the TPC-DS tables."""
    import importlib

    from tests import helpers

    with open(os.path.join(src, "tests", "test_executor.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "sqlglot":
            mod = importlib.import_module(node.module)
            for alias in node.names:
                if not hasattr(mod, alias.name):
                    importlib.import_module(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sqlglot":
                    importlib.import_module(alias.name)
    load_tables(src, "tpc-h", helpers.TPCH_SCHEMA)
    return pickle.loads(pickle.dumps(load_tables(src, "tpc-ds", helpers.TPCDS_SCHEMA)))


def check(src, expected_path):
    """Run the given queries on the patched sqlglot (served from memory) and say,
    per test, whether every one gives exactly what the pinned commit gave. Same
    output means the test's comparison against DuckDB comes out the same.

    A "front" query is one the patch never reaches from Plan() on. If its optimized
    tree and its tables are the pinned commit's, planning and execution get the same
    input and run the same code, so they are skipped. Otherwise it runs in full."""
    job = pickle.load(sys.stdin.buffer)
    sys.setrecursionlimit(20000)
    import signal

    def out_of_time(*_):
        # a patch that makes a query loop: say "kept" for everything and leave
        print("tpccheck: out of time", file=sys.stderr)
        sys.stdout.buffer.write(pickle.dumps({"_stats": {}}))
        sys.stdout.buffer.flush()
        os._exit(0)

    signal.signal(signal.SIGALRM, out_of_time)
    signal.setitimer(signal.ITIMER_REAL, job.get("front_deadline", 3600))
    sys.path.insert(0, HERE)
    from importstate import _Overlay

    sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
    with open(expected_path, "rb") as f:
        expected = pickle.load(f)
    try:
        setup_tables = class_setup(src)
    except BaseException as e:
        print("tpccheck: class setup", repr(e), file=sys.stderr)
        verdict = {test_id: False for test_id in job["jobs"]}
        verdict["_stats"] = {}
        sys.stdout.buffer.write(pickle.dumps(verdict))
        return
    sys.stdout.buffer.write(pickle.dumps(check_job(src, job, expected, setup_tables)))


def check_job(src, job, expected, setup_tables):
    """check()'s work once the patched sqlglot is in place and the tables are loaded.
    blast/runner.py calls it in a fork of its server, where both are already there.
    SIGALRM must have a handler that gives up: every query sets a time limit."""
    import signal

    verdict, stats = {}, {}
    for test_id, queries in job["jobs"].items():
        ok = True
        st = stats[test_id] = {"same_tree": 0, "ran": 0}
        try:
            install_hook()
            fixture, sqls, schema = suite(src, test_id)
            from sqlglot import find_tables, parse_one

            # TPC-DS tables come from the class setup above; others load as a query
            # first needs them
            tables = dict(setup_tables) if fixture == "tpc-ds" else {}
            cpu = job.get("cpu", {}).get(test_id)
            fcpu = job.get("front_cpu", {}).get(test_id)

            def one(q, front):
                meta, sql, _ = sqls[q]
                exp_ = expected[test_id][q]
                # a query that takes ten times its usual CPU is a patch that loops
                c = (fcpu if front else cpu) if cpu is not None and fcpu is not None else None
                if c is not None:
                    signal.setitimer(signal.ITIMER_REAL, 10 * c[q] + (3 if front else 6))
                if meta.get("execute"):
                    need = {t.name for t in find_tables(parse_one(sql))} - tables.keys()
                    if need:
                        tables.update(pickle.loads(pickle.dumps(load_tables(src, fixture, schema, need))))

                def at_plan(tree, tabs, exp_=exp_):
                    if exp_.get("tree") is not None and tree_digest(tree) == exp_["tree"] \
                            and tabs_shape(tabs, _hook["needed"]) == exp_["tabs"]:
                        raise _SameTree
                    raise _OtherTree

                res, duck = run_query(meta, sql, schema, tables, at_plan if front else None)
                if res is OTHER:
                    return None if duck == exp_["duckdb"] else False
                st["same_tree" if res is SAME else "ran"] += 1
                return not ((res is not SAME and res != exp_["result"]) or duck != exp_["duckdb"])

            # Front queries first, only as far as Plan(): about a fifth of a full run.
            # Those whose tree differs join the queries that must run in full. The
            # caller says how much CPU the full runs may cost (its estimates): once
            # they add up to more, the test stays. The first cheap query whose tree
            # differs runs in full right away: a patch that changes results usually
            # changes many, and then the rest of the pass is saved.
            full = list(queries["full"])
            budget = job.get("full_budget", {}).get(test_id, float("inf"))
            need_s = sum(cpu[q] for q in full) if cpu is not None else 0.0
            probed = set()
            order = queries["front"] if cpu is None else sorted(queries["front"], key=lambda q: (-cpu[q], q))
            for q in order:
                if need_s > budget:
                    break
                r = one(q, True)
                if r is False:
                    ok = False
                    break
                if r is None:
                    full.append(q)
                    if cpu is not None:
                        need_s += cpu[q]
                        if not probed and cpu[q] <= 0.4 and need_s <= budget:
                            probed.add(q)
                            if one(q, False) is not True:
                                ok = False
                                break
            st["full_est"] = need_s
            if ok and need_s > budget:
                print(f"tpccheck: queries to run in full come to over ~{need_s:.1f}s: over budget", file=sys.stderr)
                ok = False
            if cpu is not None:
                # cheapest first, for the same reason
                full.sort(key=lambda q: (cpu[q], q))
            full = [q for q in full if q not in probed]
            if ok:
                for q in full:
                    if one(q, False) is not True:
                        ok = False
                        break
        except Exception as e:
            print("tpccheck:", test_id, repr(e), file=sys.stderr)
            ok = False
        verdict[test_id] = ok
    verdict["_stats"] = stats
    return verdict


if __name__ == "__main__":
    cmd, src, out = sys.argv[1:4]
    src, out = os.path.abspath(src), os.path.abspath(out)
    sys.path.insert(0, src)
    os.chdir(src)
    {"map": build_map, "expected": build_expected, "check": check}[cmd](src, out)
