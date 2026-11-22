"""Per-query coverage for the TPC executor tests, and their per-query outputs.

    python blast/tpcmap.py map <sqlglot dir> <out tpc_map.pkl.gz>          (offline, committed)
    python blast/tpcmap.py expected <sqlglot dir> <out tpc_expected.pkl>  (image build)
    python blast/tpcmap.py setup-lines <sqlglot dir> <out tpc_setup_lines.json>  (image build)
    python blast/tpcmap.py check <sqlglot dir> <expected.pkl>  < pickle {"overlay", "jobs", "setup"}

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
# sqlglot is stricter once unittest or pytest is loaded (expressions/core.py UNITTEST:
# an argument a class doesn't declare raises TypeError). Every real test runs that way,
# so this process must too, before sqlglot is imported.
import unittest  # noqa: F401
import ast
import csv
import gzip
import hashlib
import os
import pickle
import re
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


ROWS_CACHE = os.path.join(HERE, "tpc_rows.pkl")
_rows_cache = []


def read_rows(src, fixture, table):
    """A fixture table's rows, parsed the way TestExecutor.setUpClass parses them."""
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
    return rows


def save_rows(src, out):
    """Image build: every fixture table's parsed rows. The check reads the pinned
    fixture files with this module's own parser, whatever the patch, so the rows it
    would parse are these; only the Table built from them runs patched code."""
    from tests import helpers

    cache = {}
    for fixture, _, schema_name in SUITES.values():
        for table in getattr(helpers, schema_name):
            cache[f"{fixture}/{table}"] = pickle.dumps(read_rows(src, fixture, table), protocol=pickle.HIGHEST_PROTOCOL)
    with open(out, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"tpc rows: {len(cache)} tables, {sum(map(len, cache.values())) / 1e6:.1f} MB", file=sys.stderr)


def cached_rows(fixture, table):
    if not _rows_cache:
        try:
            with open(ROWS_CACHE, "rb") as f:
                _rows_cache.append(pickle.load(f))
        except (OSError, pickle.PickleError, EOFError):
            _rows_cache.append({})
    blob = _rows_cache[0].get(f"{fixture}/{table}")
    return None if blob is None else pickle.loads(blob)


def load_tables(src, fixture, schema, names=None, cache=False):
    """Same parsing as TestExecutor.setUpClass, for the tables named (or all)."""
    from sqlglot.executor.table import Table

    tables = {}
    for table, columns in schema.items():
        if names is not None and table not in names:
            continue
        rows = cached_rows(fixture, table) if cache else None
        if rows is None:
            rows = read_rows(src, fixture, table)
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


class _Slow(BaseException):
    """A query ran far past its pinned CPU time (check's slow_x)."""


def _on_slow(signum, frame):
    raise _Slow
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


def tree_digest(root, skip=()):
    """The parent link and the cached hash are derived from the rest, so they are left
    out; every other slot of every node counts, because planning or execution may read it
    (skip: slots that blind_slots() found they cannot read for this query)."""
    from sqlglot import exp

    Expr = exp.Expr
    out = []
    add = out.append
    slots_of = {}
    strs = {}

    def slots(cls):
        # subclasses repeat the base's slot names: visit each name once
        names = dict.fromkeys(s for c in cls.__mro__ for s in getattr(c, "__slots__", None) or ())
        r = slots_of[cls] = (f"({cls.__module__}.{cls.__qualname__}".encode(),
                             [(s, b"|" + s.encode() + b"=") for s in names
                              if s not in ("parent", "_hash", "__dict__", "__weakref__") and s not in skip])
        return r

    def walk(v):
        t = type(v)
        if t is str:
            b = strs.get(v)
            if b is None:
                b = strs[v] = b"str:" + repr(v).encode()
            add(b)
        elif v is None:
            add(b"NoneType:None")
        elif isinstance(v, Expr):
            head, named = slots_of.get(t) or slots(t)
            add(head)
            for s, prefix in named:
                add(prefix)
                x = getattr(v, s, None)
                if x is None:
                    add(b"NoneType:None")
                else:
                    walk(x)
            d = getattr(v, "__dict__", None)
            if d:
                walk(d)
            add(b")")
        elif isinstance(v, dict):
            add(b"{")
            for k, x in v.items():
                add(repr(k).encode() + b":")
                walk(x)
            add(b"}")
        elif isinstance(v, (list, tuple)):
            add(b"[" + t.__name__.encode())
            for x in v:
                walk(x)
                add(b",")
            add(b"]")
        elif isinstance(v, (set, frozenset)):
            add(b"<" + repr(sorted(map(repr, v))).encode() + b">")
        else:
            add(t.__name__.encode() + b":" + repr(v).encode())

    walk(root)
    return hashlib.blake2b(b"".join(out), digest_size=16).hexdigest()


_SLOT_READ = re.compile(r"(?<![A-Za-z0-9_])_(type|meta)(?![A-Za-z0-9_])")
_GENERIC_READ = re.compile(r"getattr\(|setattr\(|hasattr\(|__dict__|vars\(|__slots__|__getstate__|__setstate__|__reduce"
                           r"|deepcopy|copy\.copy|pickle|\bdumps?\b|serde|attrgetter|__getattribute__")
# Lines that touch _type or _meta, or reach attributes by a generic route, yet cannot
# let them change what a query computes: Expr.__init__ sets both to None, Expr.copy and
# __deepcopy__ only carry them into the copy, and the generator's dispatch table reads
# methods off the generator class.
_CARRY = {
    "sqlglot/expressions/core.py": {
        "self._type: DataType | None = None",
        "self._meta: dict[str, t.Any] | None = None",
        "copy.comments = deepcopy(node.comments)",
        "if node._type is not None:",
        "copy._type = deepcopy(node._type)",
        "if node._meta is not None:",
        "meta = copy._meta = dict(node._meta)",
        "meta[k] = deepcopy(v)",
        "return deepcopy(self)",
    },
    "sqlglot/generator.py": {"dispatch[expr_cls] = getattr(cls, attr_name)"},
}


def blind_slots(src, exec_lines, n):
    """Per query, which of an expression's _type and _meta slots nothing reads from
    Plan() until the result is back at the pinned commit: [(meta, type)] of booleans.

    Every read of a slot is a line that names it (the `type` and `meta` properties and
    is_type() are such lines in core.py) or one of the generic routes in _GENERIC_READ,
    and Expr's hash and equality use the key and args only. So if no such line is among
    the lines a query reaches there, a run whose tree differs only in that slot executes
    the same lines on the same values: same plan, same generated code, same rows."""
    blind = [[True, True] for _ in range(n)]
    for f, lines in exec_lines.items():
        try:
            with open(os.path.join(src, f), encoding="utf-8") as fh:
                text = fh.read().splitlines()
        except OSError:
            return [(False, False)] * n
        carry = _CARRY.get(f, ())
        for l, qs in lines.items():
            line = text[l - 1].strip() if 0 < l <= len(text) else None
            if line is None:
                return [(False, False)] * n
            if line in carry:
                continue
            hit = (True, True) if _GENERIC_READ.search(line) else \
                tuple(k in {m.group(1) for m in _SLOT_READ.finditer(line)} for k in ("meta", "type"))
            if hit[0] or hit[1]:
                for q in qs:
                    if q < n:
                        blind[q][0] &= not hit[0]
                        blind[q][1] &= not hit[1]
    return [tuple(b) for b in blind]


def relaxed_digest(tree, blind):
    """The digest of a tree without the slots this query never reads after planning,
    or None when it reads both."""
    skip = tuple(s for s, b in zip(("_meta", "_type"), blind) if b)
    return tree_digest(tree, skip) if skip else None


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
    try:
        with gzip.open(os.path.join(HERE, "tpc_map.pkl.gz"), "rb") as f:
            tpc_map = pickle.load(f)
    except OSError:
        tpc_map = {}
    for test_id in SUITES:
        fixture, sqls, schema = suite(src, test_id)
        tables = load_tables(src, fixture, schema)
        rows = []
        exec_lines = tpc_map.get(test_id, {}).get("exec", {}).get("line2t")
        blind = blind_slots(src, exec_lines, len(sqls)) if exec_lines else [(False, False)] * len(sqls)
        print(f"{test_id}: {sum(b[0] for b in blind)} queries never read _meta after planning, "
              f"{sum(b[0] and b[1] for b in blind)} neither _meta nor _type", file=sys.stderr)
        for meta, sql, _ in sqls:
            seen = {}

            def at_plan(tree, tabs, blind=blind[len(rows)]):
                seen["front"] = time.process_time() - t
                seen["tree"], seen["tabs"] = tree_digest(tree), tabs_shape(tabs, _hook["needed"])
                seen["relaxed"] = relaxed_digest(tree, blind)

            def at_post():
                seen["post"] = time.process_time()

            t = time.process_time()
            res, duck = run_query(meta, sql, schema, tables, at_plan, at_post)
            end = time.process_time()
            rows.append({"result": res, "duckdb": duck, "cpu": end - t, "tree": seen.get("tree"), "tabs": seen.get("tabs"),
                         "relaxed": seen.get("relaxed"), "blind": blind[len(rows)],
                         "front": seen["front"] + end - seen["post"] if "front" in seen else end - t})
        result[test_id] = rows
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


CMP_PARTNER = {"Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt"}
CMP_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def build_cmp(src, out):
    """Image build: for every `a < b`, `a <= b`, `a > b`, `a >= b` in sqlglot, the TPC-DS
    queries in which the other operator of its pair (< with <=, > with >=) would ever
    have given a different answer or raised. sqlglot is imported with each such
    comparison rewritten into a call that evaluates both and passes the real one on.
    Query -1 stands for import and table setup. A patch that only swaps one comparison
    for its partner changes nothing in a query that is not listed for it: every
    evaluation gives the value it gave, so the run is the pinned run."""
    import builtins
    import importlib
    import operator
    import pkgutil

    sys.setrecursionlimit(20000)
    sys.path.insert(0, HERE)
    from importstate import _Overlay

    ops = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
    sites, differ, cur = [], [], [-1]

    class Rewrite(ast.NodeTransformer):
        path = None

        def visit_Compare(self, node):
            self.generic_visit(node)
            name = type(node.ops[0]).__name__
            if len(node.ops) != 1 or name not in ops:
                return node
            sites.append((self.path, node.lineno, node.col_offset, name))
            differ.append(set())
            call = ast.Call(ast.Name("blast_cmp_", ast.Load()), [ast.Constant(len(sites) - 1), node.left, node.comparators[0]], [])
            return ast.copy_location(call, node)

    def cmp(i, a, b):
        name = sites[i][3]
        try:
            real = ops[name](a, b)
        except BaseException:
            differ[i].add(cur[0])
            raise
        try:
            other = ops[CMP_PARTNER[name]](a, b)
        except BaseException:
            differ[i].add(cur[0])
            return real
        if not (type(real) is bool and real is other):
            differ[i].add(cur[0])
        return real

    builtins.blast_cmp_ = cmp
    overlay = {}
    for root, _, names in os.walk(os.path.join(src, "sqlglot")):
        for name in names:
            if name.endswith(".py"):
                full = os.path.join(root, name)
                with open(full, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                r = Rewrite()
                r.path = os.path.relpath(full, src)
                overlay[full] = ast.unparse(ast.fix_missing_locations(r.visit(tree)))
    sys.meta_path.insert(0, _Overlay(overlay))
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except BaseException:
                pass
    fixture, sqls, schema = suite(src, CMP_TEST)
    tables = pickle.loads(pickle.dumps(load_tables(src, fixture, schema)))
    load_tables(src, "tpc-h", __import__("tests.helpers", fromlist=["x"]).TPCH_SCHEMA)
    failed = 0
    for q, (meta, sql, _) in enumerate(sqls):
        cur[0] = q
        try:
            run_query(meta, sql, schema, tables)
        except Exception as e:
            failed += 1
            if failed == 1:
                print("tpc cmp: first failure", q, repr(e)[:300], file=sys.stderr)
    cur[0] = -1
    table = {}
    for (path, line, col, name), qs in zip(sites, differ):
        table.setdefault(path, {})[(line, col)] = (name, frozenset(qs))
    with open(out, "wb") as f:
        pickle.dump({CMP_TEST: table, "queries": len(sqls)}, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"tpc cmp: {len(sites)} comparisons in {len(table)} files, {sum(1 for d in differ if d)} where the partner ever differs, "
          f"{failed} queries failed", file=sys.stderr)
    if failed:
        sys.exit(1)


def class_setup(src, cache=False):
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
    load_tables(src, "tpc-h", helpers.TPCH_SCHEMA, cache=cache)
    return pickle.loads(pickle.dumps(load_tables(src, "tpc-ds", helpers.TPCDS_SCHEMA, cache=cache)))


def setup_lines(src, out):
    """Image build: the lines class_setup runs in a fresh process (sqlglot's import
    included), widened to whole statements (a multi-line statement reports only its
    first line; a def, class or if reports only its header). A patch that touches
    none of them, edits no test .py file and no TPC fixture leaves class_setup exactly
    as at the pinned commit, which passes, so the check can skip it."""
    import threading

    hit = defaultdict(set)
    root = src.rstrip("/") + "/"

    def local(frame, event, arg):
        if event == "line":
            hit[frame.f_code.co_filename].add(frame.f_lineno)
        return local

    def tracer(frame, event, arg):
        f = frame.f_code.co_filename
        if f.startswith(root):
            hit[f].add(frame.f_lineno)
            return local
        return None

    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        class_setup(src)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    lines = {}
    for f, ls in hit.items():
        rel = os.path.relpath(f, src)
        if not rel.endswith(".py"):
            continue
        with open(f, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        starts = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt):
                first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                body = getattr(node, "body", None)
                last = body[0].lineno - 1 if isinstance(body, list) and body and isinstance(body[0], ast.stmt) \
                    else node.end_lineno
                for l in range(first, max(first, last) + 1):
                    starts.setdefault(l, []).append((first, max(first, last)))
        out_lines = set()
        for l in ls:
            out_lines.add(l)
            for a, b in starts.get(l, ()):
                out_lines.update(range(a, b + 1))
        lines[rel] = sorted(out_lines)
    import json

    with open(out, "w") as fh:
        json.dump(lines, fh)
    print(f"setup lines: {sum(map(len, lines.values()))} in {len(lines)} files", file=sys.stderr)


def check(src, expected_path):
    """Run the given queries on the patched sqlglot (served from memory) and say,
    per test, whether every one gives exactly what the pinned commit gave. Same
    output means the test's comparison against DuckDB comes out the same.

    A "front" query is one the patch never reaches from Plan() on. If its optimized
    tree and its tables are the pinned commit's, planning and execution get the same
    input and run the same code, so they are skipped. Otherwise it runs in full."""
    job = pickle.load(sys.stdin.buffer)
    sys.setrecursionlimit(20000)
    sys.path.insert(0, HERE)
    from importstate import _Overlay

    sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
    with open(expected_path, "rb") as f:
        expected = pickle.load(f)
    verdict, stats = {}, {}
    # A query that takes slow_x times its pinned CPU (plus 1 s) is cut off and its test
    # kept, instead of running on until the selector's overall timeout (0: off).
    slow_x = job.get("slow_x", 0)
    relaxed = os.environ.get("BLAST_TPC_RELAXED", "1") == "1"
    early = int(os.environ.get("BLAST_TPC_EARLY", "6"))
    # not when the patch edits execute() itself: the shortcut below copies its last steps
    reuse = os.environ.get("BLAST_TPC_REUSE", "1") == "1" and "sqlglot/executor/__init__.py" not in job["overlay"]
    if slow_x:
        import signal

        signal.signal(signal.SIGPROF, _on_slow)
    slow = False
    try:
        # skipped when the selector found the patch can't change it (setup_lines)
        setup_tables = class_setup(src, cache=True) if job.get("setup", True) else {}
    except BaseException as e:
        print("tpccheck: class setup", repr(e), file=sys.stderr)
        verdict = {test_id: False for test_id in job["jobs"]}
        verdict["_stats"] = {}
        sys.stdout.buffer.write(pickle.dumps(verdict))
        return
    for test_id, queries in job["jobs"].items():
        if slow:  # state may be half-updated after a cut: keep the rest too
            verdict[test_id] = False
            continue
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

            from sqlglot.executor.python import PythonExecutor
            from sqlglot.planner import Plan

            kept = {}

            def one(q, front):
                """True: same as the pinned commit. False: differs. None (front only):
                the tree differs at Plan(), so the query must run in full to tell."""
                meta, sql, _ = sqls[q]
                exp_ = expected[test_id][q]
                if meta.get("execute"):
                    # the tables the pinned run handed on; a patched parse that wants
                    # another one finds it missing in run_query and the test stays
                    names = [n for _, n, *_ in exp_["tabs"][0]] if exp_.get("tabs") else [None]
                    need = (set(names) if None not in names else {t.name for t in find_tables(parse_one(sql))}) - tables.keys()
                    if need:
                        tables.update(pickle.loads(pickle.dumps(load_tables(src, fixture, schema, need, cache=True))))

                if not front and q in kept:
                    # The front pass already took this query to Plan() on the patched
                    # code and its DuckDB SQL matched. execute() does nothing between
                    # there and its return but plan and run, so carry on from that tree
                    # instead of parsing and optimizing the query a second time.
                    tree, tabs = kept.pop(q)
                    if slow_x:
                        signal.setitimer(signal.ITIMER_PROF, slow_x * exp_["cpu"] + 1.0)
                    try:
                        table = pickle.loads(pickle.dumps(PythonExecutor(tables=tabs).execute(Plan(tree))))
                    finally:
                        if slow_x:
                            signal.setitimer(signal.ITIMER_PROF, 0)
                    st["ran"] += 1
                    st["reused"] = st.get("reused", 0) + 1
                    return (tuple(table.columns), [tuple(r) for r in table.rows]) == exp_["result"]

                def at_plan(tree, tabs, exp_=exp_):
                    if reuse:
                        kept[q] = (tree, tabs)
                    if exp_.get("tree") is None or tabs_shape(tabs, _hook["needed"]) != exp_["tabs"]:
                        raise _OtherTree
                    if tree_digest(tree) == exp_["tree"]:
                        kept.pop(q, None)
                        raise _SameTree
                    # The tree may still differ only in slots this query never reads after
                    # planning. Copying carries those slots along, so the copies made
                    # there must work on this tree too.
                    want = exp_.get("relaxed") if relaxed else None
                    if want and relaxed_digest(tree, exp_["blind"]) == want:
                        tree.copy()
                        st["relaxed"] = st.get("relaxed", 0) + 1
                        kept.pop(q, None)
                        raise _SameTree
                    raise _OtherTree

                if slow_x:
                    usual = (exp_.get("front") if front else None) or exp_["cpu"]
                    signal.setitimer(signal.ITIMER_PROF, slow_x * usual + 1.0)
                try:
                    res, duck = run_query(meta, sql, schema, tables, at_plan if front else None)
                finally:
                    if slow_x:
                        signal.setitimer(signal.ITIMER_PROF, 0)
                if res is OTHER:
                    return None if duck == exp_["duckdb"] else False
                st["same_tree" if res is SAME else "ran"] += 1
                return not ((res is not SAME and res != exp_["result"]) or duck != exp_["duckdb"])

            # Front queries first, only as far as Plan(): about a fifth of a full run.
            # Those whose tree differs join the queries that must run in full. The
            # caller says how much CPU the full runs may cost (its estimates): once
            # they add up to more, the test stays. As soon as one tree differs, the
            # cheapest query that also differs runs in full: a patch that changes
            # results usually changes many, and then the rest of the pass is saved.
            full = list(queries["full"])
            budget = job.get("full_budget", {}).get(test_id, float("inf"))
            need_s = sum(cpu[q] for q in full) if cpu is not None else 0.0
            probed = set()
            order = list(queries["front"]) if cpu is None else sorted(queries["front"], key=lambda q: (-cpu[q], q))
            tried = [0, 0.0, 0.0]

            def note(q, r):
                if cpu is not None:
                    tried[0] += 1
                    tried[1] += cpu[q]
                    tried[2] += cpu[q] if r is None else 0.0
                return r

            while order and ok:
                if need_s > budget:
                    break
                # Heading for "over budget": if the share of tried queries whose tree
                # differs holds for the rest, the full runs will not fit. Stop and keep
                # the test now, not after the whole pass.
                if early and cpu is not None and tried[0] >= early and tried[1] > 0:
                    projected = need_s + tried[2] / tried[1] * sum(cpu[x] for x in order)
                    if projected > budget:
                        st["early"] = tried[0]
                        need_s = projected
                        break
                q = order.pop(0)
                r = note(q, one(q, True))
                if r is False:
                    ok = False
                elif r is None:
                    full.append(q)
                    if cpu is not None:
                        need_s += cpu[q]
                        if not probed and need_s <= budget:
                            for c in sorted([q] + order, key=lambda x: (cpu[x], x)):
                                if c != q:
                                    order.remove(c)
                                    r = note(c, one(c, True))
                                    if r is True:
                                        continue
                                    if r is False:
                                        ok = False
                                        break
                                    full.append(c)
                                    need_s += cpu[c]
                                probed.add(c)
                                if one(c, False) is not True:
                                    ok = False
                                break
            st["full_est"] = need_s
            if ok and need_s > budget:
                print(f"tpccheck: queries to run in full come to over ~{need_s:.1f}s: over budget", file=sys.stderr)
                st["over_budget"] = True
                ok = False
            if cpu is not None:
                # cheapest first, for the same reason as the probe
                full.sort(key=lambda q: (cpu[q], q))
            if ok:
                for q in full:
                    if q not in probed and one(q, False) is not True:
                        ok = False
                        break
        except _Slow:
            print("tpccheck:", test_id, "query far slower than at the pinned commit", file=sys.stderr)
            st["slow"] = True
            slow, ok = True, False
        except Exception as e:
            print("tpccheck:", test_id, repr(e), file=sys.stderr)
            ok = False
        verdict[test_id] = ok
    verdict["_stats"] = stats
    sys.stdout.buffer.write(pickle.dumps(verdict))


if __name__ == "__main__":
    cmd, src, out = sys.argv[1:4]
    src, out = os.path.abspath(src), os.path.abspath(out)
    sys.path.insert(0, src)
    os.chdir(src)
    {"map": build_map, "expected": build_expected, "check": check, "setup-lines": setup_lines,
     "rows": save_rows, "cmp": build_cmp}[cmd](src, out)
