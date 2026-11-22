"""Fixture-only commits: run a per-item fixture test's check on just the items the patch adds.

A few tests loop over a fixture file and check every item on its own, with nothing shared
between items (see CHECKS). Every item at the pinned commit passes, so when a patch only
edits such fixture files (sqlglot's code is untouched), the test fails exactly when one of
the items the patch adds or changes fails its check. Each check below is a copy of the
test's loop body, run against the pinned sqlglot that this process has already imported.
Any doubt (a file we don't know, a patch that doesn't apply, a check that raises
something unexpected) keeps the tests.
"""
import unittest  # noqa: F401  (loaded before sqlglot: see importstate.py)
import ast
import json
import os
import sys
from collections import Counter

# What tests/helpers.py does, kept in step with the pinned commit.


def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    sql = "\n".join(sql_lines[i:])
    return sql, meta


def load_sql_fixtures(text):
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            yield "\n".join(lines)
            lines.clear()


# The test bodies (tests/test_transpile.py, tests/test_serde.py, tests/test_optimizer.py).
# Each returns True only when the item passes the test's assertion.


def _identity(sql, ctx):
    from sqlglot import transpile

    return transpile(sql)[0] == sql.strip()


def _serde(sql, ctx):
    from sqlglot import exp, parse_one

    before = parse_one(sql)
    after = exp.Expr.load(json.loads(json.dumps(before.dump())))
    return repr(before) == repr(after)


def _partial(sql, ctx):
    from sqlglot import transpile
    from sqlglot.errors import ErrorLevel

    return transpile(sql, error_level=ErrorLevel.IGNORE)[0] == sql.strip()


def _qualify_invalid(sql, ctx):
    from sqlglot import optimizer, parse_one
    from sqlglot.errors import OptimizeError, SchemaError

    sql, meta = _extract_meta(sql)
    dialect = meta.get("dialect")
    try:
        expression = optimizer.qualify_columns.qualify_columns(
            parse_one(sql, dialect=dialect), schema=ctx.optimizer_schema(), dialect=dialect
        )
        optimizer.qualify_columns.validate_qualify_columns(expression)
    except (OptimizeError, SchemaError):
        return True
    return False


CHECKS = {
    "tests/fixtures/identity.sql": {
        "tests/test_transpile.py::TestTranspile::test_identity": _identity,
        "tests/test_serde.py::TestSerde::test_serde": _serde,
    },
    "tests/fixtures/partial.sql": {
        "tests/test_transpile.py::TestTranspile::test_partial": _partial,
    },
    "tests/fixtures/optimizer/qualify_columns__invalid.sql": {
        "tests/test_optimizer.py::TestOptimizer::test_qualify_columns__invalid": _qualify_invalid,
    },
}


class _Context:
    def __init__(self, src):
        self.src = src
        self._schema = None

    def optimizer_schema(self):
        """TestOptimizer.setUp's self.schema, read from the pinned test file (a fresh copy
        each call, as setUp builds one per test)."""
        from sqlglot import exp

        if self._schema is None:
            with open(os.path.join(self.src, "tests", "test_optimizer.py")) as f:
                tree = ast.parse(f.read())
            node = None
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef) and cls.name == "TestOptimizer":
                    for fn in cls.body:
                        if isinstance(fn, ast.FunctionDef) and fn.name == "setUp":
                            if len(fn.body) != 1:
                                raise ValueError("setUp does more than set the schema")
                            st = fn.body[0]
                            if (isinstance(st, ast.Assign) and len(st.targets) == 1
                                    and ast.unparse(st.targets[0]) == "self.schema"
                                    and isinstance(st.value, ast.Dict)):
                                node = st.value
            if node is None:
                raise ValueError("TestOptimizer.setUp schema not found")
            self._schema = compile(ast.Expression(node), "<setUp schema>", "eval")
        return eval(self._schema, {"exp": exp, "__builtins__": {}})


def judge(files, chosen, by_id, src, notes):
    """Test numbers in chosen that the patch's fixture edits provably leave passing."""
    if not files or any(fc.path not in CHECKS or fc.old_path != fc.path or fc.new_path != fc.path
                        for fc in files):
        return set()
    if len({fc.path for fc in files}) != len(files):
        return set()
    if "sqlglot" not in sys.modules and src not in sys.path:
        sys.path.insert(1, src)  # as the prover does; checked against src just below
    import sqlglot

    if not os.path.realpath(sqlglot.__file__).startswith(os.path.realpath(src) + os.sep):
        notes.append("fixture check: sqlglot is not the pinned copy")
        return set()
    chosen = set(chosen)
    ctx = _Context(src)
    safe = set()
    for fc in files:
        with open(os.path.join(src, fc.path), encoding="utf-8") as f:
            old = f.read()
        new = fc.apply(old)
        if new is None:
            notes.append(f"fixture check: {fc.path} does not apply")
            return set()
        added = list((Counter(load_sql_fixtures(new)) - Counter(load_sql_fixtures(old))).elements())
        for test_id, check in CHECKS[fc.path].items():
            n = by_id.get(test_id)
            if n is None or n not in chosen:
                continue
            try:
                ok = all(check(sql, ctx) is True for sql in added)
            except Exception as e:
                notes.append(f"fixture check: {test_id.rsplit('::', 1)[1]} raised {type(e).__name__}")
                ok = False
            notes.append(f"fixture check: {test_id.rsplit('::', 1)[1]} {len(added)} new items, "
                         f"{'all pass' if ok else 'kept'}")
            if ok:
                safe.add(n)
    return safe
