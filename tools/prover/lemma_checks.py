import difflib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "blast" / "prover"))

from prove import apply_hunks, load_packaged, split_patch  # noqa: E402

SQLGLOT = Path(sys.argv[1])
TESTS = json.loads((REPO / "sample" / "tests.json").read_text())["tests"]
NUMBER = {t["id"].split("::")[-1]: t["number"] for t in TESTS}
prover = load_packaged(REPO / "blast" / "prover_data", SQLGLOT, TESTS)
failures = []


def patch(path, edit, context=3, header=True):
    old = (SQLGLOT / path).read_text()
    new = edit(old)
    assert new != old, f"edit did not change {path}"
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=context))
    return (f"diff --git a/{path} b/{path}\n" if header else "") + body


def check(name, text, noop=None, unsafe=(), none_safe=False):
    verdict = prover.verdict(text)
    problems = []
    if noop is not None and verdict["noop"] != noop:
        problems.append(f"noop={verdict['noop']}, expected {noop}")
    if none_safe and verdict["safe"]:
        problems.append(f"claimed {len(verdict['safe'])} tests safe, expected none")
    for test in unsafe:
        if NUMBER[test] in verdict["safe"]:
            problems.append(f"{test} claimed safe: {verdict['safe'][NUMBER[test]]}")
    status = "FAIL" if problems else "ok"
    if problems:
        failures.append(name)
    print(f"{status:4} {name}: {'; '.join(problems) or verdict['notes'][:1]}")


def first_line_after(text, needle, replacement):
    return text.replace(needle, replacement, 1)


check("comment-only edit is a no-op",
      patch("sqlglot/transforms.py", lambda t: t.replace("\n\ndef ", "\n\n# note\ndef ", 1)), noop=True)
check("patch without a git header is not a no-op",
      patch("sqlglot/transforms.py", lambda t: t.replace("\n\ndef ", "\n\n# note\ndef ", 1), header=False), noop=False, none_safe=True)
check("empty patch is not a no-op", "", noop=False, none_safe=True)
check("coding cookie added is unprovable",
      patch("sqlglot/transforms.py", lambda t: "# coding: latin-1\n" + t), noop=False, none_safe=True)
check("0.0 -> -0.0 is not a no-op",
      patch("sqlglot/optimizer/simplify.py", lambda t: t.replace("0.0", "-0.0", 1) if "0.0" in t else t + "\nX = -0.0\n"), noop=False)
check("docstring edit in typing/ is not a no-op",
      patch("sqlglot/typing/__init__.py", lambda t: t.replace('"""', '""" Extra.', 1) if '"""' in t else '"""Doc."""\n' + t), noop=False)
check("tpc-ds.sql edit keeps the executor tests",
      patch("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", lambda t: t.replace(" = ", " <> ", 1)),
      noop=False, unsafe=["test_execute_tpcds", "test_tpcds", "test_agg_order"])
check("tpc-h.sql edit keeps the executor tests",
      patch("tests/fixtures/optimizer/tpc-h/tpc-h.sql", lambda t: t.replace(" = ", " <> ", 1)),
      noop=False, unsafe=["test_execute_tpch", "test_optimized_tpch", "test_tpch"])
check("identity.sql edit keeps test_identity and test_serde",
      patch("tests/fixtures/identity.sql", lambda t: t.replace("SELECT", "SELECT  ", 1)),
      noop=False, unsafe=["test_identity", "test_serde"])
check("test_dialect.py (imported by dialect tests) keeps dialect tests",
      patch("tests/dialects/test_dialect.py", lambda t: t.replace("class Validator", "class Validator  ", 1)),
      noop=False, unsafe=["test_bigquery", "test_snowflake", "test_values"])
check("helpers.py edit claims nothing",
      patch("tests/helpers.py", lambda t: t.replace("FILE_DIR", "FILE_DIR ", 1)), noop=False, none_safe=True)
check("test_optimizer.py edit keeps every optimizer heavy test",
      patch("tests/test_optimizer.py", lambda t: t.replace("def test_tpch", "def test_tpch ", 1)),
      noop=False, unsafe=["test_tpch", "test_tpcds", "test_scope", "test_simplify"])
check("file added claims nothing",
      "diff --git a/sqlglot/new.py b/sqlglot/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/new.py\n@@ -0,0 +1 @@\n+X = 1\n",
      noop=False, none_safe=True)
check("non-python file outside tests claims nothing",
      "diff --git a/setup.py b/setup.py\n--- a/setup.py\n+++ b/setup.py\n@@ -1 +1 @@\n-x\n+y\n", noop=False, none_safe=True)
check("syntax error claims nothing",
      patch("sqlglot/transforms.py", lambda t: t.replace("\ndef ", "\ndef (", 1)), noop=False, none_safe=True)
check("WEEK_OFFSET class constant claims nothing for test_simplify",
      patch("sqlglot/dialects/tsql.py", lambda t: t.replace("WEEK_OFFSET = -1", "WEEK_OFFSET = -2", 1)),
      noop=False, unsafe=["test_simplify"])

zero_context = patch("sqlglot/transforms.py", lambda t: t.replace("\n\ndef ", "\n\nX_INSERTED = 1\ndef ", 1), context=0)
entry = split_patch(zero_context)[0]
old_text = (SQLGLOT / "sqlglot/transforms.py").read_text()
expected = old_text.replace("\n\ndef ", "\n\nX_INSERTED = 1\ndef ", 1)
ok = apply_hunks(old_text, entry["hunks"]) == expected
print(f"{'ok' if ok else 'FAIL':4} zero-context insertion applies at the right line")
if not ok:
    failures.append("zero-context insertion")

print(f"\n{len(failures)} failing checks")
sys.exit(1 if failures else 0)
