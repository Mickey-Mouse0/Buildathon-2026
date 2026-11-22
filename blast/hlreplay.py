"""test_max_nodes and edits to sqlglot.errors.highlight_sql.

The test (28.8 s, the slowest one) reaches errors.py only through highlight_sql, a pure
function of its arguments, which it calls a few times while building ParseError messages.
At image build we record those calls (arguments and results) by running the test itself
(`record`). When a patch only changes the body of highlight_sql, a child process runs the
patched errors.py on the recorded arguments (`replay`): if every call returns the same
result, the test sees exactly what it saw at the pinned commit. One looser rule, for this
test only: raise_error unpacks the 4 strings into the message after the text the test
checks, so 4 strings of any content also leave it passing. Anything else keeps the test.

    python hlreplay.py record <sqlglot src> <out.pkl>     (image build, ~30 s)
"""
import ast
import builtins
import copy
import os
import pickle
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDING = os.environ.get("BLAST_HL_RECORDING", os.path.join(HERE, "maxnodes_hl.pkl"))
TEST_ID = "tests/test_parser.py::TestParser::test_max_nodes"
ERRORS = "sqlglot/errors.py"
FUNC = "highlight_sql"
# builtins the body may name: none of them reach state outside the call
SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "int", "isinstance", "len",
    "list", "map", "max", "min", "range", "reversed", "set", "sorted", "str", "sum", "tuple",
    "zip", "ValueError", "IndexError", "TypeError", "KeyError", "True", "False", "None",
}


def _static_check(old_src, new_src):
    """None if the patch changes only highlight_sql's body, in a way whose effect is
    limited to its result; else why not."""
    try:
        o, n = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return "does not parse"
    if len(o.body) != len(n.body):
        return "module statements added or removed"
    target = None
    for a, b in zip(o.body, n.body):
        if ast.dump(a) == ast.dump(b):
            continue
        if (isinstance(a, ast.FunctionDef) and isinstance(b, ast.FunctionDef) and a.name == b.name == FUNC
                and ast.dump(a.args) == ast.dump(b.args)
                and [ast.dump(d) for d in a.decorator_list] == [ast.dump(d) for d in b.decorator_list]):
            target = b
            continue
        return "a change outside highlight_sql's body"
    if target is None:
        return "no change"
    # names the body may read: its parameters, its own locals, a few builtins, and module
    # constants (strings and numbers, unchanged since only this body differs)
    consts = set()
    for st in n.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            val = st.value
            if isinstance(val, ast.Constant) and isinstance(val.value, (str, int)):
                for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                    if isinstance(t, ast.Name):
                        consts.add(t.id)
    params = {a.arg for a in target.args.args + target.args.kwonlyargs + target.args.posonlyargs}
    stored = set()
    for node in ast.walk(target):
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom, ast.Yield, ast.YieldFrom,
                             ast.Await, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not target:
            return f"body has {type(node).__name__}"
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stored.add(node.id)
        elif isinstance(node, ast.arg):
            stored.add(node.arg)  # lambda parameters
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"body reads .{node.attr}"
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in params | stored | consts | SAFE_BUILTINS:
                return f"body names {node.id}"
    # writes through attributes or subscripts only to the function's own locals
    for node in ast.walk(target):
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in stored - params):
                return "body writes to something it did not create"
    return None


def judge(files, src, notes):
    """True when test_max_nodes is sure to pass with this patch (it passes at the pinned
    commit and touches nothing else the patch changes)."""
    if len(files) != 1 or files[0].path != ERRORS or files[0].old_path != ERRORS or files[0].new_path != ERRORS:
        return False
    if not os.path.exists(RECORDING):
        return False
    with open(os.path.join(src, ERRORS), encoding="utf-8", newline="") as f:
        old = f.read()
    new = files[0].apply(old)
    if new is None or "�" in new:
        return False
    why = _static_check(old, new)
    if why is not None:
        notes.append(f"max_nodes replay: {why}")
        return False
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "replay", src, RECORDING],
                           input=new.encode(), capture_output=True, timeout=5,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        verdict = r.stdout.decode(errors="replace").strip() if r.returncode == 0 else "child failed"
    except subprocess.TimeoutExpired:
        verdict = "timed out"
    notes.append(f"max_nodes replay: {verdict}")
    return verdict in ("same", "same shape")


def _replay(src, rec_path, new_src):
    sys.path.insert(0, src)
    import types

    with open(rec_path, "rb") as f:
        calls = pickle.load(f)
    if not calls:
        return "no recorded calls"
    mod = types.ModuleType("sqlglot_errors_patched")
    mod.__file__ = os.path.join(src, ERRORS)
    exec(compile(new_src, mod.__file__, "exec"), mod.__dict__)
    fn = getattr(mod, FUNC)
    before = dict(mod.__dict__)
    exact = True
    for sql, positions, ctx, want, secs in calls:
        args = copy.deepcopy(positions)
        t0 = time.process_time()
        try:
            got = fn(sql, args, ctx)
        except BaseException as e:
            return f"raised {type(e).__name__}"
        if time.process_time() - t0 > max(0.05, 100 * secs):
            return "slow"
        if args != positions:
            return "changed its positions argument"
        if got != want:
            exact = False
            if not (type(got) is tuple and len(got) == 4 and all(type(x) is str for x in got)):
                return f"result is a {type(got).__name__}"
    if mod.__dict__.keys() != before.keys() or any(mod.__dict__[k] is not v for k, v in before.items()):
        return "changed module globals"
    return "same" if exact else "same shape"


def _record(src, out):
    """Run test_max_nodes as the pinned commit and record every highlight_sql call."""
    import importlib
    import pkgutil
    import unittest

    os.chdir(src)
    sys.path.insert(0, src)
    import sqlglot
    import sqlglot.errors as errors

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception:
                pass
    orig = errors.highlight_sql
    calls = []

    def rec(sql, positions, context_length=errors.ERROR_MESSAGE_CONTEXT_DEFAULT):
        pos = copy.deepcopy(positions)
        t0 = time.process_time()
        result = orig(sql, positions, context_length)  # a raise propagates and fails the test
        calls.append((sql, pos, context_length, result, time.process_time() - t0))
        return result

    bound = 0
    for name, m in list(sys.modules.items()):
        if (name == "sqlglot" or name.startswith("sqlglot.")) and getattr(m, FUNC, None) is orig:
            setattr(m, FUNC, rec)
            bound += 1
    from tests.test_parser import TestParser

    res = unittest.TextTestRunner(verbosity=0).run(TestParser("test_max_nodes"))
    if not res.wasSuccessful() or not calls or any(type(c[3]) is not tuple for c in calls):
        sys.exit(f"recording failed: ok={res.wasSuccessful()} calls={len(calls)}")
    with open(out, "wb") as f:
        pickle.dump(calls, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"recorded {len(calls)} highlight_sql calls ({bound} bindings)", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "record":
        _record(os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]))
    elif sys.argv[1] == "replay":
        try:
            print(_replay(sys.argv[2], sys.argv[3], sys.stdin.buffer.read().decode()))
        except BaseException as e:
            print(f"replay error {type(e).__name__}")
