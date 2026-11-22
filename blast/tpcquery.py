"""Run the queries an edit to tests/fixtures/optimizer/tpc-ds/tpc-ds.sql changes, the
way test_execute_tpcds does, and say whether that test still passes.

    python blast/tpcquery.py <sqlglot dir> <deps dir>  < pickle {"text": patched file}

Pickles {"ok": bool, "why": str} to stdout.

    python blast/tpcquery.py known <sqlglot dir> <out json>

Run at image build: lists the expression types the pinned file's executed queries use
(see order_is_fixed), so the check doesn't parse all 99 queries again on every run.

The test loads (meta, query, expected) triples from the file and, for each one, sends
parse_one(query) to a pool worker (mp_execute) and compares the result with DuckDB
running the same query (subtestHelper). Each triple is handled on its own, so a
triple whose meta and query are unchanged does what it did at the pinned commit,
where the test passes. Only the changed ones are run here, with the test module's
own code: its setUpClass, mp_execute and subtestHelper.

Passing here must also mean passing where the answers were labeled. Both engines
must therefore have only one possible output for the changed query, so on top of the
comparison the query has to pin its row order down completely (see order_is_fixed).
Anything unclear is "not ok", and the test stays selected.

DuckDB, pandas and numpy are installed for this check alone, in <deps dir>, which
nothing else puts on its path.
"""
import math
import os
import pickle
import sys
import tempfile

FILE = "optimizer/tpc-ds/tpc-ds.sql"
KNOWN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpc_known.json")

# Expressions whose value is not fixed by the data, or depends on row order.
UNFIXED = {
    "Window", "Anonymous", "TableSample", "Rand", "Randn", "Uuid", "CurrentDate", "CurrentTime", "CurrentTimestamp",
    "CurrentDatetime", "AnyValue", "First", "Last", "FirstValue", "LastValue", "ArrayAgg", "GroupConcat", "ArbitraryValue",
}


def _same(a, b):
    import pandas as pd

    if a is b:
        return True
    try:
        if pd.isna(a) and pd.isna(b):
            return True
    except (TypeError, ValueError):
        pass
    try:
        return bool(a == b)
    except Exception:
        return False


def _tied(a, b):
    """Could the two sort key values compare equal on some machine?"""
    if _same(a, b):
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
        except (TypeError, ValueError):
            return True
    return False


def _qual(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


def known_types(sqls):
    """The expression types (by qualified name) of the pinned file's executed queries."""
    from sqlglot import parse_one

    known = set()
    for meta, sql, _ in sqls:
        if meta.get("execute"):
            known.update(_qual(type(n)) for n in parse_one(sql).walk())
    return known


def build_known(src, out):
    import json

    sys.path.insert(0, src)
    os.chdir(src)
    import tests.helpers as H

    with open(out, "w") as f:
        json.dump(sorted(known_types(H.load_sql_fixture_pairs(FILE))), f, indent=0)
        f.write("\n")


def order_is_fixed(sql, conn, frame, known_types):
    """None if the query can only ever return these rows in this order, else why not.

    It holds when the query is a plain SELECT made of the kinds of expression the
    pinned file's executed queries use, with no window, no inner LIMIT, and an ORDER
    BY over output columns under which no two different rows tie, including the
    first row a LIMIT cuts off. Without ORDER BY, only a result of at most one row.
    """
    from sqlglot import exp, parse_one

    e = parse_one(sql)
    if type(e) is not exp.Select:
        return "not a plain SELECT"
    limit = e.args.get("limit")
    for node in e.walk():
        name = type(node).__name__
        if name in UNFIXED:
            return f"uses {name}"
        if _qual(type(node)) not in known_types:
            return f"uses {name}, which no pinned query uses"
        if isinstance(node, (exp.Limit, exp.Fetch, exp.Offset)) and node is not limit:
            return "inner LIMIT or OFFSET"
    order = e.args.get("order")
    if order is None:
        if limit is not None or len(frame) > 1:
            return "more than one row and no ORDER BY"
        return None
    names = [p.alias_or_name for p in e.expressions]
    if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in e.expressions):
        return "star projection"
    if len(names) != len(frame.columns):
        return "output columns don't line up with the SELECT list"
    keys = []
    for o in order.expressions:
        k = o.this
        if isinstance(k, exp.Literal) and k.is_int and 1 <= int(k.this) <= len(names):
            keys.append(int(k.this) - 1)
        elif isinstance(k, exp.Column) and not k.table and names.count(k.name) == 1:
            keys.append(names.index(k.name))
        else:
            hits = [i for i, p in enumerate(e.expressions) if (p.unalias() if isinstance(p, exp.Alias) else p) == k]
            if len(hits) != 1:
                return "an ORDER BY key is not an output column"
            keys.append(hits[0])
    full = frame
    if limit is not None:
        n = limit.expression
        if type(limit) is not exp.Limit or not (isinstance(n, exp.Literal) and n.is_int):
            return "LIMIT is not a plain number"
        n = int(n.this)
        if len(frame) > n:
            return "more rows than the LIMIT"
        if len(frame) == n:
            # one more row, to see whether the cut falls between two tied rows
            more = e.copy()
            more.args["limit"].set("expression", exp.Literal.number(n + 1))
            full = conn.execute(more.sql(dialect="duckdb")).fetchdf()
            if len(full) < n:
                return "LIMIT n+1 gave fewer rows"
    rows = list(full.itertuples(index=False, name=None))
    head = list(frame.itertuples(index=False, name=None))
    if any(not all(_same(x, y) for x, y in zip(a, b)) for a, b in zip(head, rows)):
        return "a second run gave different rows"
    for a, b in zip(rows, rows[1:]):
        if all(_tied(a[i], b[i]) for i in keys) and not all(_same(x, y) for x, y in zip(a, b)):
            return "two different rows tie in the ORDER BY"
    return None


def main():
    src, deps = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    job = pickle.load(sys.stdin.buffer)
    out = os.dup(1)
    os.dup2(2, 1)  # nothing may reach our stdout but the answer
    sys.path.insert(0, src)
    sys.path.append(deps)
    os.chdir(src)
    os.environ["SKIP_INTEGRATION"] = "0"
    why = None
    try:
        import tests.helpers as H
        import tests.test_executor as TE

        T = TE.TestExecutor
        # setUpClass loads the TPC-H tables too; test_execute_tpcds never reads them
        tpch = TE.TPCH_SCHEMA
        TE.TPCH_SCHEMA = {}
        try:
            T.setUpClass()
        finally:
            TE.TPCH_SCHEMA = tpch
        old = T.tpcds_sqls
        # the patched file, through the test's own loader
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, FILE)
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(job["text"])
            real = H.FIXTURES_DIR
            H.FIXTURES_DIR = tmp
            try:
                new = list(H.load_sql_fixture_pairs(FILE))
            finally:
                H.FIXTURES_DIR = real
        T.tpcds_sqls = new
        seen = {(tuple(meta.items()), sql) for meta, sql, _ in old}
        changed = [i for i, (meta, sql, _) in enumerate(new) if (tuple(meta.items()), sql) not in seen]
        try:
            import json

            with open(KNOWN) as f:
                known = set(json.load(f))
        except OSError:
            known = known_types(old)
        TE.initializer(*pickle.loads(pickle.dumps((TE.TPCDS_SCHEMA, T.tpcds_tables))))
        case = T("test_execute_tpcds")
        for i in changed:
            meta, sql, _ = new[i]
            expression = pickle.loads(pickle.dumps(TE.parse_one(sql)))
            table = pickle.loads(pickle.dumps(TE.mp_execute(expression, meta)))
            if table is None:
                continue
            case.subtestHelper(i, table, tpch=False)
            why = order_is_fixed(sql, T.tpcds_conn, T.cache[sql], known)
            if why is not None:
                why = f"query {i + 1}: {why}"
                break
        ok = why is None
        if ok:
            why = f"{len(changed)} changed queries pass"
    except BaseException as e:  # an assertion from the comparison lands here too
        ok, why = False, f"{type(e).__name__}: {str(e)[:120]}"
    with os.fdopen(out, "wb") as f:
        f.write(pickle.dumps({"ok": ok, "why": why}))


if __name__ == "__main__":
    if sys.argv[1] == "known":
        build_known(os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]))
    else:
        main()
