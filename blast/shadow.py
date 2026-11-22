"""Which tests can see a one-line edit, from one instrumented run of each test.

A patch that changes one piece of code can only change a test in which that piece,
at some evaluation, does something the original would not have done. At image build
(runcheck.py shadow) sqlglot is imported with these places rewritten to report in:

  cmp  a single comparison: every evaluation (E), and for <, <=, >, >=, is, is not
       whether the operator a swap would put there (<= for <, == for is, ...) would
       have given another answer (D)
  bin  a + b or a - b: every evaluation (E), and whether the other sign differs (D)
  reg  code that runs only on demand: the later operands of and/or, the two arms of
       x if c else y, the element and filters of a comprehension, a lambda's body (E)
  def  a parameter whose default is a bool, int or str constant: every call that
       came in with a value equal to that default (E)

Each test, and each TPC-DS query, runs in its own fork of the instrumented process.
units() then answers, for a patch that replaces one line inside one function, which
of them the change can reach; None when it cannot say.
"""
import ast
import os
import pickle
import textwrap

PARTNER = {"Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt", "Is": "Eq", "IsNot": "NotEq"}
NEGATE = {"Eq": "NotEq", "NotEq": "Eq", "In": "NotIn", "NotIn": "In", "Is": "IsNot", "IsNot": "Is"}
CMP = set(PARTNER) | set(NEGATE)
SIGN = {"Add": "Sub", "Sub": "Add"}
FORBIDDEN = (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)


def span(n):
    return n.lineno, n.col_offset, n.end_lineno, n.end_col_offset


def regions(tree):
    """Expression nodes that are evaluated only on demand."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp):
            out += node.values[1:]
        elif isinstance(node, ast.IfExp):
            out += [node.body, node.orelse]
        elif isinstance(node, ast.Lambda):
            out.append(node.body)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            out += [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for g in node.generators:
                out += g.ifs
    return out


def default_params(fn):
    """[(parameter name, default Constant)] for bool, int and str defaults."""
    a = fn.args
    pos = a.posonlyargs + a.args
    pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + \
        [(p, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
    return [(p.arg, d) for p, d in pairs if isinstance(d, ast.Constant) and type(d.value) in (bool, int, str)]


class _Rewrite(ast.NodeTransformer):
    def __init__(self, path, sites, tree):
        self.path, self.sites = path, sites
        self.regs = {id(n) for n in regions(tree)}

    def add(self, *site):
        self.sites.append(site)
        return len(self.sites) - 1

    def call(self, name, i, *args):
        return ast.Call(ast.Name(name, ast.Load()), [ast.Constant(i), *args], [])

    def visit(self, node):
        rid = self.add("reg", self.path, span(node), None) if id(node) in self.regs else None
        new = super().visit(node)
        if rid is not None:
            pair = ast.Tuple([self.call("blast_ev_", rid), new], ast.Load())
            new = ast.copy_location(ast.Subscript(pair, ast.Constant(1), ast.Load()), node)
        return new

    def visit_Compare(self, node):
        key = (node.lineno, node.col_offset)
        self.generic_visit(node)
        name = type(node.ops[0]).__name__
        if len(node.ops) != 1 or name not in CMP:
            return node
        return ast.copy_location(self.call("blast_cmp_", self.add("cmp", self.path, key, name), node.left, node.comparators[0]), node)

    def visit_BinOp(self, node):
        key = (node.lineno, node.col_offset)
        self.generic_visit(node)
        name = type(node.op).__name__
        if name not in SIGN:
            return node
        return ast.copy_location(self.call("blast_bin_", self.add("bin", self.path, key, name), node.left, node.right), node)

    def _function(self, node):
        params = default_params(node)
        ids = [self.add("def", self.path, (node.lineno, name), const.value) for name, const in params]
        self.generic_visit(node)
        if params:
            stmt = ast.Expr(self.call("blast_df_", ids[0], *[ast.Name(name, ast.Load()) for name, _ in params]))
            first = node.body[0]
            doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
            node.body.insert(1 if doc else 0, ast.copy_location(stmt, first))
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _function


def instrument(src, sites):
    """{absolute path: rewritten source} for every sqlglot module, filling sites with
    (kind, path, key, extra)."""
    overlay = {}
    for root, dirs, names in os.walk(os.path.join(src, "sqlglot")):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".py"):
                full = os.path.join(root, name)
                with open(full, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                new = _Rewrite(os.path.relpath(full, src), sites, tree).visit(tree)
                overlay[full] = ast.unparse(ast.fix_missing_locations(new))
    return overlay


SIMPLE = (type(None), bool, int, float, str)
PAIR_CAP = 12


def hooks(sites, hit, pairs):
    """The four functions the rewritten code calls. hit gets 2*i for E and 2*i+1 for D.
    pairs[i] collects the distinct operand pairs a comparison saw while both sides were
    None, bool, int, float or a short str; None once it saw anything else or too many."""
    import operator

    order = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
    plain = (type(None), bool, int, str, bytes)
    inert = plain + (float, list, tuple, dict, set, frozenset)

    def identity_is_equality(a, b):
        """Is `a == b` sure to equal `a is b`, with nothing run that could have an effect?"""
        ta, tb = type(a), type(b)
        if a is b:
            return ta in plain or ta.__eq__ is object.__eq__ or ta.__module__.startswith("sqlglot")
        if ta.__eq__ is object.__eq__ and tb.__eq__ is object.__eq__:
            return True
        if a is None or b is None:
            t = tb if a is None else ta
            return t in inert or t.__module__.startswith("sqlglot")
        if ta in plain and tb in plain:
            return not (a == b)
        return False

    def cmp(i, a, b):
        name = sites[i][3]
        hit.add(2 * i)
        seen = pairs.get(i, ())
        if seen is not None:
            ta, tb = type(a), type(b)
            if ta in SIMPLE and tb in SIMPLE and (ta is not str or len(a) <= 40) and (tb is not str or len(b) <= 40) \
                    and a == a and b == b:
                if seen == ():
                    seen = pairs[i] = set()
                seen.add((ta.__name__, a, tb.__name__, b))
                if len(seen) > PAIR_CAP:
                    pairs[i] = None
            else:
                pairs[i] = None
        if name == "Is" or name == "IsNot":
            if not identity_is_equality(a, b):
                hit.add(2 * i + 1)
            return (a is b) if name == "Is" else (a is not b)
        if name == "Eq":
            return a == b
        if name == "NotEq":
            return a != b
        if name == "In":
            return a in b
        if name == "NotIn":
            return a not in b
        try:
            real = order[name](a, b)
        except BaseException:
            hit.add(2 * i + 1)
            raise
        try:
            other = order[PARTNER[name]](a, b)
        except BaseException:
            hit.add(2 * i + 1)
            return real
        if not (type(real) is bool and real is other):
            hit.add(2 * i + 1)
        return real

    def binop(i, a, b):
        hit.add(2 * i)
        if not (type(a) is int and type(b) is int and b == 0):
            hit.add(2 * i + 1)
        return a + b if sites[i][3] == "Add" else a - b

    def ev(i):
        hit.add(2 * i)

    def df(i, *values):
        for k, v in enumerate(values):
            c = sites[i + k][3]
            if type(v) is type(c) and v == c:
                hit.add(2 * (i + k))

    return {"blast_cmp_": cmp, "blast_bin_": binop, "blast_ev_": ev, "blast_df_": df}


def package(src, sites, at_import, per_unit, values, units, recorded, out):
    """Write the recording: per file, where each site is and which units hit it.
    values[i] maps a tuple of operand pairs (or None) to the units that saw exactly it."""
    files = {}
    for i, (kind, path, key, extra) in enumerate(sites):
        t = files.setdefault(path, {"cmp": {}, "bin": {}, "reg": {}, "def": {}, "hits": {}, "import": set(), "funcs": [], "pairs": {}})
        if i in values:
            t["pairs"][i] = {k: frozenset(v) for k, v in values[i].items()}
        # two sites at one position can't be told apart from a patch: neither is used
        t[kind][key] = None if key in t[kind] else (extra, i)
        for code in (2 * i, 2 * i + 1):
            if code in at_import:
                t["import"].add(code)
            if code in per_unit:
                t["hits"][code] = frozenset(per_unit[code])
    for path, t in files.items():
        with open(os.path.join(src, path), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        # the pieces of source units() parses on their own: every function, and every
        # other statement that sits directly in a module or a class body
        blocks = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for holder in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            blocks += [n for n in holder.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        t["funcs"] = sorted((n.lineno, n.end_lineno) for n in blocks)
    with open(out, "wb") as f:
        pickle.dump({"units": units, "recorded": frozenset(recorded),
                     "files": {p: pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL) for p, t in files.items()}},
                    f, protocol=pickle.HIGHEST_PROTOCOL)


def _replay(table, chain, at):
    """Units a comparison can come out differently in, when the patch changed nothing
    but its operator and/or a constant operand and the swap is not one hooks() already
    judged exactly: the new comparison is worked out on every operand pair each unit
    saw. None when this is not such a patch or the site was never recorded this way."""
    import operator

    x, y = chain[-1]
    pair = None
    for cx, cy in reversed(chain[-2:]):
        if isinstance(cx, ast.Compare) and isinstance(cy, ast.Compare):
            pair = (cx, cy)
            break
    if pair is None or len(pair[0].ops) != 1 or len(pair[1].ops) != 1:
        return None
    c, d = pair
    was, now = type(c.ops[0]).__name__, type(d.ops[0]).__name__
    sides = []
    for old, new in ((c.left, d.left), (c.comparators[0], d.comparators[0])):
        if ast.dump(old) == ast.dump(new):
            sides.append(None)
        elif isinstance(old, ast.Constant) and isinstance(new, ast.Constant) and type(old.value) in SIMPLE and type(new.value) in SIMPLE:
            sides.append((old.value, new.value))
        else:
            return None
    if sides == [None, None] and (PARTNER.get(was) == now or NEGATE.get(was) == now):
        return None  # judged exactly by the E and D sets
    entry = table["cmp"].get(at(c))
    if entry is None or entry[0] != was or 2 * entry[1] in table["import"]:
        return None
    seen = table["pairs"].get(entry[1])
    if seen is None:
        return None
    fns = {"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
           "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b}
    single = (type(None), bool)

    def run(name, a, b):
        if name in ("Is", "IsNot"):
            # identity of two recorded values is only known against None, True and False
            if type(a) in single:
                same = type(b) is type(a) and b == a
            elif type(b) in single:
                same = False
            else:
                raise ValueError
            return same if name == "Is" else not same
        return fns[name](a, b)

    reach = set()
    evaluated = table["hits"].get(2 * entry[1], frozenset())
    known = set()
    for values, us in seen.items():
        known |= us
        differs = values is None
        for ta, a, tb, b in values or ():
            a2, b2 = a, b
            if sides[0] is not None:
                if type(a) is not type(sides[0][0]) or a != sides[0][0]:
                    differs = True
                    break
                a2 = sides[0][1]
            if sides[1] is not None:
                if type(b) is not type(sides[1][0]) or b != sides[1][0]:
                    differs = True
                    break
                b2 = sides[1][1]
            try:
                r1, r2 = run(was, a, b), run(now, a2, b2)
            except Exception:
                differs = True
                break
            if not (type(r1) is bool and r1 is r2):
                differs = True
                break
        if differs:
            reach |= us
    return reach | (evaluated - known)


_loaded = {}


def _table(recording, path):
    if recording not in _loaded:
        try:
            with open(recording, "rb") as f:
                _loaded[recording] = (pickle.load(f), {})
        except (OSError, pickle.PickleError, EOFError):
            _loaded[recording] = (None, {})
    data, cache = _loaded[recording]
    if data is None or path not in data["files"]:
        return None, None
    if path not in cache:
        cache[path] = pickle.loads(data["files"][path])
    return data, cache[path]


def units(recording, src, fc):
    """(names of the units this one-file patch can reach, names of the recorded units,
    every unit's name), or None. fc is a patchutil.FileChange for a sqlglot module."""
    if fc.old_path is None or fc.old_path != fc.new_path or not (fc.path.startswith("sqlglot/") and fc.path.endswith(".py")):
        return None
    if len(fc.hunks) != 1:
        return None
    start, _, lines = fc.hunks[0]
    minus = [k for k, ln in enumerate(lines) if ln.startswith("-")]
    plus = [k for k, ln in enumerate(lines) if ln.startswith("+")]
    if len(minus) != 1 or len(plus) != 1:
        return None
    data, table = _table(recording, fc.path)
    if table is None:
        return None
    line = start + sum(1 for ln in lines[:minus[0]] if ln.startswith(" ") or ln == "")
    inside = [(s, e) for s, e in table["funcs"] if s <= line <= e]
    if not inside:
        return None
    s, e = max(inside)  # the innermost block: the latest to start
    with open(os.path.join(src, fc.old_path), encoding="utf-8") as f:
        old = f.read().split("\n")
    if old[line - 1] != lines[minus[0]][1:].rstrip("\n"):
        return None
    new = list(old)
    new[line - 1] = lines[plus[0]][1:].rstrip("\n")
    seg_old, seg_new = "\n".join(old[s - 1:e]), "\n".join(new[s - 1:e])
    indent = len(old[s - 1]) - len(old[s - 1].lstrip())
    ded_old, ded_new = textwrap.dedent(seg_old), textwrap.dedent(seg_new)
    if len(seg_old.split("\n")[0]) - len(ded_old.split("\n")[0]) != indent or \
            len(seg_new.split("\n")[0]) - len(ded_new.split("\n")[0]) != indent:
        return None
    try:
        a, b = ast.parse(ded_old), ast.parse(ded_new)
    except SyntaxError:
        return None
    # walk down to the one subtree that differs
    chain, x, y = [], a, b
    while True:
        chain.append((x, y))
        if type(x) is not type(y):
            break
        diffs, same = [], True
        for (_, vx), (_, vy) in zip(ast.iter_fields(x), ast.iter_fields(y)):
            if isinstance(vx, ast.AST) and isinstance(vy, ast.AST):
                if ast.dump(vx) != ast.dump(vy):
                    diffs.append((vx, vy))
            elif isinstance(vx, list) and isinstance(vy, list):
                if len(vx) != len(vy):
                    same = False
                    break
                for cx, cy in zip(vx, vy):
                    if isinstance(cx, ast.AST) and isinstance(cy, ast.AST):
                        if ast.dump(cx) != ast.dump(cy):
                            diffs.append((cx, cy))
                    elif cx != cy:
                        same = False
            elif vx != vy:
                same = False
        if not same or len(diffs) != 1:
            break
        x, y = diffs[0]
    if len(chain) < 2:
        return None
    x, y = chain[-1]
    if any(isinstance(n, FORBIDDEN) for n in ast.walk(y)) and not any(isinstance(n, FORBIDDEN) for n in ast.walk(x)):
        return None
    up = [p[0] for p in chain]

    def at(n, full=False):
        k = (n.lineno + s - 1, n.col_offset + indent)
        return k + (n.end_lineno + s - 1, n.end_col_offset + indent) if full else k

    code = None
    parent = up[-2]
    replayed = _replay(table, chain, at)
    if replayed is not None:
        names = data["units"]
        return {names[u] for u in replayed}, {names[u] for u in data["recorded"]}, names
    if isinstance(x, ast.cmpop) and isinstance(parent, ast.Compare) and len(parent.ops) == 1:
        entry = table["cmp"].get(at(parent))
        was, now = type(x).__name__, type(y).__name__
        if entry is not None and entry[0] == was:
            code = 2 * entry[1] + 1 if PARTNER.get(was) == now else 2 * entry[1] if NEGATE.get(was) == now else None
    elif isinstance(x, ast.operator) and isinstance(parent, ast.BinOp):
        entry = table["bin"].get(at(parent))
        was, now = type(x).__name__, type(y).__name__
        if entry is not None and entry[0] == was and SIGN.get(was) == now:
            code = 2 * entry[1] + 1
    elif isinstance(x, ast.Constant) and isinstance(y, ast.Constant) and isinstance(parent, ast.arguments) and len(up) >= 3 \
            and isinstance(up[-3], (ast.FunctionDef, ast.AsyncFunctionDef)):
        name = next((n for n, d in default_params(up[-3]) if d is x), None)
        entry = table["def"].get((up[-3].lineno + s - 1, name)) if name is not None else None
        if entry is not None and type(entry[0]) is type(x.value) and entry[0] == x.value:
            code = 2 * entry[1]
    if code is None:
        regs = {id(n) for n in regions(a)}
        for n in reversed(up):
            if id(n) in regs:
                entry = table["reg"].get(at(n, True))
                if entry is not None:
                    code = 2 * entry[1]
                break
    if code is None or code in table["import"]:
        return None
    names = data["units"]
    return {names[u] for u in table["hits"].get(code, ())}, {names[u] for u in data["recorded"]}, names
