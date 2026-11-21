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


def run_query(meta, sql, schema, tables):
    """What the test computes for one query: (result columns and rows or None, duckdb sql)."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute

    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return None, None  # the test only parses and pickles it
    needed = {t.name: tables[t.name] for t in find_tables(expression)}
    table = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=needed)))
    return (tuple(table.columns), [tuple(r) for r in table.rows]), transpile(sql, write="duckdb")[0]


def build_map(src, out):
    os.environ.setdefault("COVMAP_OUT", "/tmp/unused")
    os.environ["COVMAP_ROOT"] = src
    sys.path.insert(0, TOOLS)
    import covmap_plugin as P
    import importlib
    import pkgutil

    mon = P.mon
    mon.use_tool_id(P.TOOL, "covmap")
    mon.register_callback(P.TOOL, mon.events.LINE, P.on_line)
    mon.set_events(P.TOOL, mon.events.LINE)
    import builtins
    import io

    builtins.open = P.rec_open
    io.open = P.rec_open
    P.state["cur"] = P.importtime
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception:
                pass
    P.state["cur"] = None
    P.wrap_class_attrs()
    P.snapshot_globals()

    result = {}
    for test_id in SUITES:
        common = [set(), set(), set()]
        P.state["cur"], P.state["opened"], P.state["attrs"] = common
        mon.restart_events()
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        pickle.loads(pickle.dumps((schema, tables)))  # the pool's initargs
        per_q = []
        for meta, sql, _ in sqls:
            P.reset_globals()
            b = [set(), set(), set()]
            P.state["cur"], P.state["opened"], P.state["attrs"] = b
            mon.restart_events()
            try:
                run_query(meta, sql, schema, tables)
            except Exception as e:  # it passes at the pinned commit; just in case
                print("query failed", test_id, len(per_q), repr(e), file=sys.stderr)
            P.state["cur"], P.state["opened"], P.state["attrs"] = None, None, None
            per_q.append([b[0] | common[0], b[1] | common[1], b[2] | common[2]])
        line2t = defaultdict(lambda: defaultdict(set))
        open2t, attr2t, cls2t = defaultdict(set), defaultdict(set), defaultdict(set)
        for i, (lines, opened, attrs) in enumerate(per_q):
            for f, l in lines:
                line2t[f][l].add(i)
            for f in opened:
                open2t[f].add(i)
            for k in attrs:
                attr2t[k].add(i)
                cls2t[k.rsplit(".", 1)[0]].add(i)
        result[test_id] = {
            "tests": [f"q{i + 1}" for i in range(len(per_q))],
            "line2t": {f: {l: frozenset(s) for l, s in m.items()} for f, m in line2t.items()},
            "open2t": {f: frozenset(s) for f, s in open2t.items()},
            "attr2t": {k: frozenset(s) for k, s in attr2t.items()},
            "cls2t": {k: frozenset(s) for k, s in cls2t.items()},
            "executes": [bool(meta.get("execute")) for meta, _, _ in sqls],
        }
        print(f"{test_id}: {len(per_q)} queries", file=sys.stderr)
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def build_expected(src, out):
    import time

    result = {}
    for test_id in SUITES:
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        rows = []
        for meta, sql, _ in sqls:
            t = time.process_time()
            res, duck = run_query(meta, sql, schema, tables)
            rows.append({"result": res, "duckdb": duck, "cpu": time.process_time() - t})
        result[test_id] = rows
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def check(src, expected_path):
    """Run the given queries on the patched sqlglot (served from memory) and say,
    per test, whether every one gives exactly what the pinned commit gave. Same
    output means the test's comparison against DuckDB comes out the same."""
    job = pickle.load(sys.stdin.buffer)
    sys.path.insert(0, HERE)
    from importstate import _Overlay

    sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
    with open(expected_path, "rb") as f:
        expected = pickle.load(f)
    verdict = {}
    for test_id, queries in job["jobs"].items():
        ok = True
        try:
            fixture, sqls, schema = suite(src, test_id)
            from sqlglot import find_tables, parse_one

            names = set()
            for q in queries:
                if sqls[q][0].get("execute"):
                    names |= {t.name for t in find_tables(parse_one(sqls[q][1]))}
            tables = load_tables(src, fixture, schema, names)
            pickle.loads(pickle.dumps((schema, tables)))
            for q in queries:
                meta, sql, _ = sqls[q]
                res, duck = run_query(meta, sql, schema, tables)
                exp_ = expected[test_id][q]
                if res != exp_["result"] or duck != exp_["duckdb"]:
                    ok = False
                    break
        except Exception as e:
            print("tpccheck:", test_id, repr(e), file=sys.stderr)
            ok = False
        verdict[test_id] = ok
    sys.stdout.buffer.write(pickle.dumps(verdict))


if __name__ == "__main__":
    cmd, src, out = sys.argv[1:4]
    src, out = os.path.abspath(src), os.path.abspath(out)
    sys.path.insert(0, src)
    os.chdir(src)
    {"map": build_map, "expected": build_expected, "check": check}[cmd](src, out)
