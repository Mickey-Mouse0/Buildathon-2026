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

Most of a query's cost is the Python executor walking the data. A patch that only
reaches a query's front half (parse, optimize, transpile) can be checked without
executing: if the front half hands the executor exactly what it did at the pinned
commit, and no changed line runs in the back half, the back half runs as before.
"map" therefore also records each query's back half on its own (the "exec" map),
and "expected" a fingerprint of what the front half hands over. See front() and
back() for where the line between them is.
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


# execute() lives here. front() and back() repeat its body, split in two, so they
# only stand for it while this file is unchanged: any patch to it means "back half".
EXEC_INIT = "sqlglot/executor/__init__.py"


def parsed(sql):
    """The expression as the pool worker receives it."""
    from sqlglot import parse_one

    return pickle.loads(pickle.dumps(parse_one(sql)))


def table_names(expression):
    """The tables mp_execute hands to execute(), in its order."""
    from sqlglot import find_tables

    return list(dict.fromkeys(t.name for t in find_tables(expression)))


def front(expression, schema):
    """The part of execute() that never looks at the data: schema and optimize.
    Returns everything the rest of execute() reads from it."""
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    schema = ensure_schema(schema, dialect=None)
    return schema.supported_table_args, optimize(expression, schema, leave_tables_isolated=True, dialect=None)


def back_tables(needed):
    """execute()'s first step. It only reads the tables it is given."""
    from sqlglot.executor.table import ensure_tables

    return ensure_tables(needed, dialect=None)


def back(tables_, table_args, expression):
    """The rest of execute() after optimize, and the result's trip back from the pool."""
    from sqlglot.errors import ExecuteError
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.planner import Plan

    if tables_.supported_table_args and tables_.supported_table_args != table_args:
        raise ExecuteError("Tables must support the same table args as schema")
    expression.sql(pretty=True)  # execute() builds this string for a debug log line
    plan = Plan(expression)
    return pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))


def run_staged(meta, sql, schema, tables, switch=lambda half: None):
    """run_query in execute()'s own order, calling switch("front" | "back") whenever
    the work crosses the line. Returns (result, duckdb sql, what front handed over)."""
    from sqlglot import transpile

    switch("front")
    expression = parsed(sql)
    if not meta.get("execute"):
        return None, None, None
    names = table_names(expression)
    switch("back")
    tables_ = back_tables({n: tables[n] for n in names})
    switch("front")
    table_args, optimized = front(expression, schema)
    handed = fingerprint((names, table_args, optimized))
    switch("back")
    table = back(tables_, table_args, optimized)
    switch("front")
    duck = transpile(sql, write="duckdb")[0]
    return (tuple(table.columns), [tuple(r) for r in table.rows]), duck, handed


def run_front(meta, sql, schema):
    """Only the front half: (duckdb sql, what it hands over). No data is loaded."""
    from sqlglot import transpile

    expression = parsed(sql)
    if not meta.get("execute"):
        return None, None
    names = table_names(expression)
    table_args, optimized = front(expression, schema)
    handed = fingerprint((names, table_args, optimized))
    return transpile(sql, write="duckdb")[0], handed


FRONT_GIVE_UP = 3


class Unreadable(Exception):
    pass


def fingerprint(value):
    """A digest of everything reachable from value: every attribute of every object,
    with sharing and cycles kept (a second visit to an object says which one it was).
    Two equal digests mean the two object graphs cannot be told apart by code that
    reads them. None when part of it is of a kind we cannot read exactly."""
    import datetime
    import decimal
    import enum
    import hashlib
    import types

    h = hashlib.blake2b(digest_size=16)
    put = h.update
    seen = {}
    plain = (datetime.date, datetime.time, datetime.timedelta, decimal.Decimal, range, complex)
    stack = [value]
    try:
        while stack:
            v = stack.pop()
            t = type(v)
            if v is None or t is bool or t is int or t is float or t is str or t is bytes:
                put(f"{t.__name__}:{v!r};".encode("utf-8", "surrogatepass"))
            elif t is _Mark:
                put(v.text)
            elif isinstance(v, enum.Enum):
                put(f"E:{t.__module__}.{t.__qualname__}.{v.name};".encode())
            elif isinstance(v, (type, types.FunctionType, types.BuiltinFunctionType)):
                if "<locals>" in getattr(v, "__qualname__", "<locals>"):
                    raise Unreadable(repr(v))
                put(f"R:{v.__module__}.{v.__qualname__};".encode())
            elif t in plain:
                put(f"P:{t.__name__}:{v!r};".encode())
            elif t is list or t is tuple:
                put(f"{t.__name__}[{len(v)}".encode())
                stack.extend(reversed(v))
            elif t is dict:
                put(f"dict[{len(v)}".encode())
                for k, x in reversed(v.items()):
                    stack.append(x)
                    stack.append(k)
            elif t is set or t is frozenset:
                # order is not part of a set, so only sets of simple values can be read
                if not all(x is None or type(x) in (bool, int, float, str, bytes) for x in v):
                    raise Unreadable("set of objects")
                put(f"{t.__name__}{sorted(map(repr, v))!r};".encode("utf-8", "surrogatepass"))
            else:
                if t.__module__ != "sqlglot" and not t.__module__.startswith("sqlglot."):
                    raise Unreadable(f"{t.__module__}.{t.__qualname__}")
                n = seen.get(id(v))
                if n is not None:
                    put(f"@{n};".encode())
                    continue
                seen[id(v)] = len(seen)
                names = set(getattr(v, "__dict__", ()))
                for klass in t.__mro__:
                    slots = klass.__dict__.get("__slots__", ())
                    names.update((slots,) if isinstance(slots, str) else slots)
                names.discard("__dict__")
                names.discard("__weakref__")
                put(f"O:{t.__module__}.{t.__qualname__}[{len(names)}".encode())
                for name in sorted(names, reverse=True):
                    try:
                        stack.append(getattr(v, name))
                    except AttributeError:
                        stack.append(_Mark(b"<unset>;"))
                    stack.append(_Mark(f".{name}=".encode()))
    except Unreadable as e:
        print("tpcmap: cannot fingerprint", e, file=sys.stderr)
        return None
    return h.digest()


class _Mark:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


def _shape(per_q, sqls):
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
    return {
        "tests": [f"q{i + 1}" for i in range(len(per_q))],
        "line2t": {f: {l: frozenset(s) for l, s in m.items()} for f, m in line2t.items()},
        "open2t": {f: frozenset(s) for f, s in open2t.items()},
        "attr2t": {k: frozenset(s) for k, s in attr2t.items()},
        "cls2t": {k: frozenset(s) for k, s in cls2t.items()},
        "executes": [bool(meta.get("execute")) for meta, _, _ in sqls],
    }


def build_map(src, out):
    os.environ.setdefault("COVMAP_OUT", "/tmp/unused")
    os.environ["COVMAP_ROOT"] = src
    sys.path.insert(0, TOOLS)
    import covmap_plugin as P

    mon = P.mon
    P.start()

    def record_into(b):
        P.state["cur"], P.state["opened"], P.state["attrs"] = b
        mon.restart_events()

    result = {}
    for test_id in SUITES:
        common = [set(), set(), set()]
        record_into(common)
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        pickle.loads(pickle.dumps((schema, tables)))  # the pool's initargs
        per_q, per_back = [], []
        for meta, sql, _ in sqls:
            P.reset_globals()
            b = [set(), set(), set()]
            record_into(b)
            try:
                run_query(meta, sql, schema, tables)
            except Exception as e:  # it passes at the pinned commit; just in case
                print("query failed", test_id, len(per_q), repr(e), file=sys.stderr)
            per_q.append([b[0] | common[0], b[1] | common[1], b[2] | common[2]])

            # Again, split: what runs in the back half goes to its own bucket. The
            # per-process caches are emptied at every crossing, so a cache the front
            # half filled is filled again, and credited, where the back half reads it.
            back_b, front_b = [set(), set(), set()], [set(), set(), set()]

            def switch(half):
                P.reset_globals()
                record_into(back_b if half == "back" else front_b)

            try:
                run_staged(meta, sql, schema, tables, switch)
            except Exception as e:
                print("staged query failed", test_id, len(per_back), repr(e), file=sys.stderr)
                back_b = b  # everything counts as back half
            P.state["cur"], P.state["opened"], P.state["attrs"] = None, None, None
            per_back.append([back_b[0] | common[0], back_b[1] | common[1], back_b[2] | common[2]])
        result[test_id] = _shape(per_q, sqls)
        result[test_id]["exec"] = _shape(per_back, sqls)
        print(f"{test_id}: {len(per_q)} queries", file=sys.stderr)
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def build_expected(src, out):
    """Run under PYTHONHASHSEED=0, as the check is: the fingerprints hold cached hashes."""
    import time

    result = {}
    for test_id in SUITES:
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        rows = []
        for meta, sql, _ in sqls:
            t = time.process_time()
            res, duck = run_query(meta, sql, schema, tables)
            cpu = time.process_time() - t
            # The split run must give what the real one gives, and the front half
            # alone must hand over the same thing, or this query is never checked
            # by its front half.
            t = time.process_time()
            try:
                duck2, handed = run_front(meta, sql, schema)
            except Exception:
                duck2, handed = None, None
            cpu_front = time.process_time() - t
            if handed is not None:
                try:
                    staged = run_staged(meta, sql, schema, tables)
                except Exception:
                    staged = None
                if staged != (res, duck, handed) or duck2 != duck:
                    print("tpcmap: front half not usable for", test_id, len(rows), file=sys.stderr)
                    handed = None
            rows.append({"result": res, "duckdb": duck, "cpu": cpu, "front": handed, "cpu_front": cpu_front})
        result[test_id] = rows
        print(f"{test_id}: {sum(r['front'] is not None for r in rows)} of {len(rows)} queries have a front fingerprint",
              file=sys.stderr)
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def confirm(src, out):
    """A second process must get the same fingerprints. One that differs between two
    runs of the pinned commit says nothing about a patch, so it is dropped."""
    with open(out, "rb") as f:
        result = pickle.load(f)
    for test_id, rows in result.items():
        fixture, sqls, schema = suite(src, test_id)
        dropped = 0
        for (meta, sql, _), row in zip(sqls, rows):
            if row["front"] is not None and run_front(meta, sql, schema) != (row["duckdb"], row["front"]):
                row["front"] = None
                dropped += 1
        print(f"{test_id}: {dropped} front fingerprints differ in a second process", file=sys.stderr)
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


def check(src, expected_path):
    """Say, per test, whether the patched sqlglot (served from memory) does for every
    given query what the pinned commit did. job["front"] lists queries the patch only
    reaches in their front half: for those, the same DuckDB SQL and the same hand-over
    to the executor. job["jobs"] lists the rest: the same result table and DuckDB SQL.
    Either way the test's comparison against DuckDB comes out the same."""
    job = pickle.load(sys.stdin.buffer)
    sys.path.insert(0, HERE)
    from importstate import _Overlay

    sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
    with open(expected_path, "rb") as f:
        expected = pickle.load(f)
    verdict = {}
    for test_id in {**job.get("front", {}), **job["jobs"]}:
        ok = True
        try:
            fixture, sqls, schema = suite(src, test_id)
            from sqlglot import find_tables, parse_one

            # tables load as a query first needs them, so a patch that changes an early
            # query's result stops the check before the rest are loaded or run
            tables = {}

            def same_result(q):
                meta, sql, _ = sqls[q]
                if meta.get("execute"):
                    need = {t.name for t in find_tables(parse_one(sql))} - tables.keys()
                    if need:
                        tables.update(pickle.loads(pickle.dumps(load_tables(src, fixture, schema, need))))
                res, duck = run_query(meta, sql, schema, tables)
                exp_ = expected[test_id][q]
                return res == exp_["result"] and duck == exp_["duckdb"]

            def same_front(q):
                meta, sql, _ = sqls[q]
                exp_ = expected[test_id][q]
                try:
                    return exp_["front"] is not None and run_front(meta, sql, schema) == (exp_["duckdb"], exp_["front"])
                except Exception:
                    return False

            # A front half that hands over something else may still give the same
            # result (an equivalent plan), so that query is then run in full.
            # After a few of those in a row the patch plainly changes plans without
            # changing results, and the front half is no longer worth trying first.
            differ = 0
            for q in job.get("front", {}).get(test_id, ()):
                if differ < FRONT_GIVE_UP and same_front(q):
                    differ = 0
                    continue
                differ += 1
                if not same_result(q):
                    ok = False
                    break
            for q in job["jobs"].get(test_id, ()) if ok else ():
                if not same_result(q):
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
    {"map": build_map, "expected": build_expected, "confirm": confirm, "check": check}[cmd](src, out)
