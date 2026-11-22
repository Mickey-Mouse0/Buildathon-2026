"""Static analysis of a patch on top of the coverage map.

For each changed line we work out what kind of code it is, then ask the map
which tests could see it:

- inside a function body: tests that ran the changed statement (all its lines)
- a function's def line, decorator or defaults: tests that ran the function
- a lambda inside a class-body table: tests that ran the lambda
- a class setting (class-body assignment): tests that read that attribute, plus
  settings derived from it in other class bodies; if import-time code reads it
  (metaclasses, __init_subclass__), every test that uses the class
- a module-level constant: tests that ran a function mentioning its name
- a test data file: tests that opened it
- a test file: the tests in it

When anything is unclear we return None, which means "run everything".
"""
import ast
import gzip
import os
import pickle

from keyreads import dict_change

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
DIALECT_DIRS = ("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.",
                "sqlglot.tokenizers.", "sqlglot.typing.")


# TestExecutor.setUpClass loads both TPC query files into class attributes, so the map
# credits each file to every TestExecutor test. Only these tests read the attribute
# holding that file's queries (self.tpch_sqls / self.tpcds_sqls).
EXECUTOR = "tests/test_executor.py::TestExecutor::"
TPC_SQL = {
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": ("test_optimized_tpch", "test_execute_tpch"),
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": ("test_execute_tpcds",),
}

# Of those, the ones that ignore the expected side of each pair.
TPC_EXECUTE_ONLY = ("test_execute_tpch", "test_execute_tpcds")


def tpc_pairs_load(text):
    """What tests/helpers.load_sql_fixture_pairs does with a file's text, at the pinned
    commit: raises exactly when it would raise inside setUpClass. Returns the (meta,
    sql) of each pair, which is all the execute tests read; the expected side is not."""
    statements = "\n".join(l for l in text.splitlines() if l and not l.startswith("--")).split(";")
    pairs = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            lines = statements[i].strip().split("\n")
            meta = {}
            j = 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            pairs.append((list(meta.items()), "\n".join(lines[j:])))
    return pairs


class _LazyLines:
    """line2t with each file's table unpickled the first time it is asked for: a run
    touches a few dozen files, and the whole table is half of the map's load time."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.loaded = {}

    def get(self, path, default=None):
        hit = self.loaded.get(path)
        if hit is None:
            raw = self.blobs.get(path)
            if raw is None:
                return default
            hit = self.loaded[path] = pickle.loads(raw)
        return hit


def split_map(path):
    """Image build: rewrite the map uncompressed, with line2t kept as one pickle per file."""
    with gzip.open(path, "rb") as f:
        d = pickle.load(f)
    d["line2t"] = {k: pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL) for k, v in d["line2t"].items()}
    with open(path + ".split", "wb") as f:
        pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)


class Maps:
    def __init__(self, path, src):
        if os.path.exists(path + ".split"):
            with open(path + ".split", "rb") as f:
                d = pickle.load(f)
            d["line2t"] = _LazyLines(d["line2t"])
        else:
            with gzip.open(path, "rb") as f:
                d = pickle.load(f)
        self.__dict__.update(d)
        self.src = src
        self.all = frozenset(range(len(self.tests)))

    def lines(self, path, lines):
        m = self.line2t.get(path)
        out = set()
        if m:
            for l in lines:
                s = m.get(l)
                if s:
                    out |= s
        return out

    def func_range(self, path, start, end):
        return self.lines(path, range(start, end + 1))

    def class_usage(self, ck):
        out = set(self.cls2t.get(ck, ()))
        for sc in self.subclasses.get(ck, ()):
            out |= self.cls2t.get(sc, set())
        return out


def same_module(old, oldt, new, newt):
    """ast.dump(oldt) == ast.dump(newt), without dumping all of a 9,000-line file:
    statements whose source text is unchanged parse the same, so only the others are
    compared, descending into class and function bodies."""
    ol, nl = old.splitlines(), new.splitlines()

    def text(lines, st):
        start = st.decorator_list[0].lineno if getattr(st, "decorator_list", None) else st.lineno
        return lines[start - 1:st.end_lineno]

    def same_body(xs, ys):
        return len(xs) == len(ys) and all(same(x, y) for x, y in zip(xs, ys))

    def same(x, y):
        if text(ol, x) == text(nl, y):
            return True
        if type(x) is not type(y):
            return False
        if isinstance(x, (ast.ClassDef,) + FUNC):
            hx = ast.dump(type(x)(**{**{f: getattr(x, f) for f in x._fields}, "body": []}))
            hy = ast.dump(type(y)(**{**{f: getattr(y, f) for f in y._fields}, "body": []}))
            return hx == hy and same_body(x.body, y.body)
        return ast.dump(x) == ast.dump(y)

    return same_body(oldt.body, newt.body)


def stmt_chain(tree, line):
    """Statements (outermost first) whose span holds the line."""
    chain = []
    body = tree.body
    while True:
        found = None
        for st in body:
            start = st.decorator_list[0].lineno if getattr(st, "decorator_list", None) else st.lineno
            if start <= line <= st.end_lineno:
                found = st
                break
        if found is None:
            return chain
        chain.append(found)
        body = []
        for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
            v = getattr(found, fld, None)
            if isinstance(v, list):
                body.extend(v)
        if not body:
            return chain


def header_lines(st):
    """A statement's own lines, without its nested blocks."""
    nested = []
    for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
        v = getattr(st, fld, None)
        if isinstance(v, list):
            nested.extend(v)
    if not nested:
        return range(st.lineno, st.end_lineno + 1)
    return range(st.lineno, max(st.lineno + 1, min(n.lineno for n in nested)))


def targets_of(st):
    if isinstance(st, ast.Assign):
        tg = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        tg = [st.target]
    else:
        return set()
    return {x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)}


class Analyzer:
    def __init__(self, maps, import_diff=False, keyreads=None, plain_setting=None):
        self.m = maps
        self.keyreads = keyreads
        # plain_setting(class key, name): whether the pinned commit's import leaves that
        # class attribute as plain data (not an enum member, a callable or a module)
        self.plain_setting = plain_setting
        # With import_diff, changes whose effect is decided while sqlglot is imported
        # are not answered here. They are left in self.deferred for the selector,
        # which compares sqlglot's state after import with and without the patch
        # (importstate.py) and maps what differs through diff_tests().
        self.import_diff = import_diff
        self.deferred = None

    def defer(self, why, fallback, notes, attrs=()):
        """Leave an import-time question for the import diff. fallback = the tests to
        take if the diff can't run (None: all of them)."""
        d = self.deferred
        if d is None:
            d = self.deferred = {"fallback": set(), "all": False, "why": [], "attrs": set()}
        d["why"].append(why)
        d["attrs"] |= set(attrs)
        if fallback is None:
            d["all"] = True
        else:
            d["fallback"] |= fallback
        notes.append(f"deferred to import diff: {why}")

    # ---- class settings -------------------------------------------------
    def class_attr(self, ck, names, notes, constant=False):
        m = self.m
        keys = {f"{ck}.{n}" for n in names}
        own_keys = set(keys)
        cnames = {ck.split(":")[1].split(".")[-1]}
        seen = set(names)
        work = list(names)
        import_read = False
        while work:
            n = work.pop()
            for e in m.names.get(n, ()):
                if e[0] == "classbody":
                    _, mod, q, tg, refs = e
                    same = f"{mod}:{q}" == ck
                    if not ((None, n) in refs and same or any(b in cnames for b, a in refs if a == n and b)):
                        continue
                    for t in tg:
                        keys.add(f"{mod}:{q}.{t}")
                        cnames.add(q.split(".")[-1])
                        if t not in seen:
                            seen.add(t)
                            work.append(t)
                elif e[0] == "func":
                    _, path, start, body, end = e
                    imp = m.importtime.get(path, ())
                    if any(l in imp for l in range(body, end + 1)):
                        import_read = True
        out = set()
        for k in keys:
            out |= m.attr2t.get(k, set())
        notes.append(f"attr {','.join(sorted(names))}: {len(out)}")
        if import_read:
            mod, qual = ck.split(":")
            parts = qual.split(".")
            u = set()
            for i in range(1, len(parts) + 1):
                u |= m.class_usage(f"{mod}:{'.'.join(parts[:i])}")
            if (mod + ".").startswith(DIALECT_DIRS):
                for c in m.cls2t:
                    if c.startswith(mod + ":"):
                        u |= m.class_usage(c)
            notes.append(f"read at import, class users: {len(u)}")
            if self.import_diff:
                self.defer(f"{ck} {','.join(sorted(names))} read at import", u, notes)
            else:
                out |= u
        if constant and not import_read and keys == own_keys:
            # One setting went from one constant to another, nothing copies it in a
            # class body and no import-time code mentions it, so its value is all that
            # differs after import. The import diff would find just that and answer
            # with these readers; importing the patched sqlglot to hear it is wasted.
            for k in own_keys:
                name = k.rsplit(".", 1)[1]
                for sc in m.subclasses.get(ck, ()):
                    out |= m.attr2t.get(f"{sc}.{name}", set())
            notes.append(f"constant setting: {len(out)} (no import diff needed)")
            return out
        if self.import_diff and self.keyreads is not None:
            # The import diff says which settings really changed (a replaced line also
            # marks the setting below it here) and, for dicts, which keys; the key map
            # says who read those keys. Settings it can't fingerprint keep these readers.
            self.defer(f"{ck} {','.join(sorted(names))} settings", out, notes, attrs=keys)
            return set()
        return out

    # ---- TPC query files ---------------------------------------------------
    def tpc_sql(self, fc, path, tests, notes):
        """Drop the TestExecutor tests that never read this file's queries, unless the
        edited file would make setUpClass itself fail (then the whole class fails)."""
        m = self.m
        try:
            with open(os.path.join(m.src, path), encoding="utf-8") as f:
                old = f.read()
            new = fc.apply(old)
            if new is None or "\ufffd" in new:
                return tests
            pairs = tpc_pairs_load(new)
        except Exception:
            notes.append("TPC file breaks setUpClass: whole class")
            return tests
        keep = {EXECUTOR + n for n in TPC_SQL[path]}
        # The execute tests read only each pair's meta and query (mp_execute, then
        # cached_execute on the same query). If every pair has the same ones, in the
        # same order, the edit is on the expected side, which they never look at.
        try:
            if pairs == tpc_pairs_load(old):
                keep -= {EXECUTOR + n for n in TPC_EXECUTE_ONLY}
                notes.append("TPC file: queries unchanged, execute tests dropped")
        except Exception:
            pass
        out = {i for i in tests if not (m.tests[i].startswith(EXECUTOR) and m.tests[i] not in keep)}
        notes.append(f"TPC file: {len(tests) - len(out)} executor tests dropped")
        return out

    # ---- test files ------------------------------------------------------
    def _test_importers(self, path):
        """Test files that import this one (`from tests.x.y import ...`), transitively."""
        if not hasattr(self, "_timp"):
            imp = {}
            root = os.path.join(self.m.src, "tests")
            for dp, _, fns in os.walk(root):
                for fn in fns:
                    if fn.endswith(".py"):
                        f = os.path.relpath(os.path.join(dp, fn), self.m.src)
                        with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                t = line.split()
                                if len(t) >= 2 and t[0] in ("from", "import") and t[1].startswith("tests."):
                                    imp.setdefault(t[1].replace(".", "/") + ".py", set()).add(f)
            self._timp = imp
        seen, todo = set(), [path]
        while todo:
            for f in self._timp.get(todo.pop(), ()):
                if f not in seen:
                    seen.add(f)
                    todo.append(f)
        return seen

    def test_file(self, fc, path, s, notes):
        """Tests an edit to a test module can change. When only test_* methods differ,
        just those tests (and same-named ones in subclasses elsewhere); otherwise the
        file's tests plus those of every test file importing it."""
        m = self.m
        with open(os.path.join(m.src, path), encoding="utf-8") as f:
            old = f.read()
        new = fc.apply(old)
        if new is None:
            notes.append("patch does not apply")
            return None
        try:
            oldt, newt = ast.parse(old), ast.parse(new)
        except SyntaxError:
            notes.append(f"test file {path} broken: whole file")
            return set(s)

        def split(tree):
            rest, meths = [], {}
            for st in tree.body:
                if isinstance(st, ast.ClassDef):
                    body = []
                    for x in st.body:
                        if isinstance(x, FUNC) and x.name.startswith("test"):
                            meths[(st.name, x.name)] = ast.dump(x)
                        else:
                            body.append(x)
                    rest.append(ast.dump(ast.ClassDef(st.name, st.bases, st.keywords, body, st.decorator_list, [])))
                elif isinstance(st, FUNC) and st.name.startswith("test"):
                    meths[(None, st.name)] = ast.dump(st)
                else:
                    rest.append(ast.dump(st))
            return rest, meths

        orest, ometh = split(oldt)
        nrest, nmeth = split(newt)
        if orest != nrest:
            out = set(s)
            imps = self._test_importers(path)
            for f in imps:
                out |= m.tests_by_file.get(f, set())
            notes.append(f"test file {path}: {len(out)} (helpers changed; {len(imps)} importing files)")
            return out
        changed = {k for k in ometh if ometh[k] != nmeth.get(k)}
        if not hasattr(m, "_test_ids"):
            ids = {}
            for i, t in enumerate(m.tests):
                ids.setdefault(t.split("[")[0].rsplit("::", 1)[-1], []).append(i)
            m._test_ids = ids
        out = set()
        own = {f"{path}::{c}::{n}" if c else f"{path}::{n}" for c, n in changed}
        names = {n for _, n in changed}
        importers = self._test_importers(path)
        for n in names:
            for i in m._test_ids.get(n, ()):
                t = m.tests[i].split("[")[0]
                # this file's test, or the same method inherited by a class elsewhere
                if t in own or t.split("::")[0] in importers:
                    out.add(i)
        notes.append(f"test file {path}: {len(out)} of {len(s)} (changed tests {sorted(names)[:4]})")
        return out

    # ---- module constants -----------------------------------------------
    def module_global(self, path, names, notes, depth=0, own_file=False, funcs_only=False):
        """own_file: only code in this module can see the name (a module object
        bound by `import x`: nothing reaches it as othermodule.x).
        funcs_only: only code that reads it at run time. For the import diff, which
        lists every class setting and module global built from it as its own entry."""
        m = self.m
        out = set()
        modname = path[:-3].replace("/", ".").removesuffix(".__init__")
        gnames = getattr(m, "gnames", None)
        wn = getattr(m, "wnames", None) or {}
        fimp, attr = wn.get("<fromimports>"), wn.get("<attr>", {})

        def reach(f, key):
            """Can code in file f see this module's binding of n? Only through
            `from <module> import n` (or *), or as an attribute / a string. In the
            import diff every module's binding is an entry of its own, so a bare n
            elsewhere is that module's own (it changes there if its value does)."""
            if fimp is None:
                return True
            fi = fimp.get(f)
            if fi is None or (modname, n) in fi or (modname, "*") in fi:
                return True
            return key is not None and key in attr.get(n, ())

        for n in names:
            if funcs_only:
                lams = wn.get("<lambdas>")
                if lams is None:
                    funcs_only = False  # an older map: follow class bodies as before
                else:
                    # lambdas in class or module bodies read n when called
                    for f, a, b, as_attr in lams.get(n, ()):
                        if f == path or as_attr or reach(f, None):
                            out |= m.lines(f, range(a, b + 1))
            g = gnames.get(n, frozenset()) if gnames is not None else None
            for e in m.names.get(n, ()):
                if own_file and e[1] != (modname if e[0] == "classbody" else path):
                    continue
                if e[0] == "func":
                    if g is not None and (e[1], e[2]) not in g:
                        continue  # mentions it only as self.x / param.x: not this global
                    if funcs_only and e[1] != path and not reach(e[1], (e[1], e[2])):
                        continue  # another module's own binding of the same name
                    out |= m.func_range(e[1], e[2], e[4])
                elif funcs_only:
                    continue
                elif e[0] == "classbody":
                    _, mod, q, tg, refs = e
                    if (None, n) in refs and tg:
                        out |= self.class_attr(f"{mod}:{q}", tg, notes)
                elif e[0] == "module" and depth < 3:
                    _, p, tg = e
                    tg = set(tg) - set(names)
                    if tg:
                        out |= self.module_global(p, tg, notes, depth + 1)
        notes.append(f"global {','.join(sorted(names))}: {len(out)}")
        return out

    def _star_imported(self, mod):
        if not hasattr(self, "_stars"):
            stars = set()
            for top in ("sqlglot", "tests"):
                for dp, _, fns in os.walk(os.path.join(self.m.src, top)):
                    for fn in fns:
                        if fn.endswith(".py"):
                            with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as f:
                                for line in f:
                                    line = line.strip()
                                    if line.startswith("from ") and line.split("#")[0].rstrip().endswith("import *"):
                                        stars.add(line.split()[1])
            self._stars = stars
        return mod in self._stars

    # ---- python source -----------------------------------------------------
    def python_file(self, fc, notes):
        m = self.m
        path = fc.old_path
        if path is None:
            notes.append(f"new file {fc.new_path}")
            return set()  # nothing imports a brand new module unless another file changes
        with open(os.path.join(m.src, path)) as f:
            old = f.read()
        new = fc.apply(old)
        if new is None:
            notes.append("patch does not apply")
            return None
        oldt = ast.parse(old)
        try:
            newt = ast.parse(new)
        except SyntaxError:
            notes.append("syntax error")
            return None
        if same_module(old, oldt, new, newt):
            notes.append(f"no-op {path}")
            return set()
        changed = set(fc.removed_lines())
        for l in fc.insert_points():
            changed |= {l - 1, l}
        modname = path[:-3].replace("/", ".").removesuffix(".__init__")
        out = set()
        swap = None
        removed = sorted(fc.removed_lines())
        if len(removed) == 1:
            chain = stmt_chain(oldt, removed[0])
            quals = [s.name for s in chain if isinstance(s, ast.ClassDef)]
            if quals and self._constant_swap(old, new, newt, chain, removed[0], f"{modname}:{'.'.join(quals)}"):
                swap = removed[0]
        for l in sorted(changed):
            if swap is not None and l != swap:
                # the lines around a one-line replacement are marked too, but here the
                # old and new statements both start and end on that line
                continue
            chain = stmt_chain(oldt, l)
            if not chain:
                continue
            fidx = max((i for i, s in enumerate(chain) if isinstance(s, FUNC)), default=None)
            if fidx is not None:
                fn = chain[fidx]
                start = fn.decorator_list[0].lineno if fn.decorator_list else fn.lineno
                if fidx == len(chain) - 1 or l < fn.body[0].lineno:
                    out |= m.func_range(path, fn.body[0].lineno, fn.end_lineno)
                    notes.append(f"def {fn.name}")
                    if any(isinstance(s, ast.ClassDef) for s in chain[:fidx]) or fidx == 0:
                        if fn.decorator_list and start <= l < fn.lineno:
                            notes.append("decorator")
                    continue
                own = header_lines(chain[-1])
                imp = m.importtime.get(path, ())
                if any(x in imp for x in own):
                    # This code runs while sqlglot is imported (metaclasses, table
                    # builders, helpers called from class bodies), so what it computes
                    # is baked in before any test starts and per-test coverage can't
                    # see who depends on it.
                    outer = next(s for s in chain if isinstance(s, FUNC))
                    nested = [n for n in ast.walk(outer) if n is not outer and isinstance(n, FUNC + (ast.Lambda,))]
                    if outer.name.startswith("__") or not nested or any(
                        n.lineno <= l <= n.end_lineno for n in nested
                    ):
                        notes.append(f"runs at import: {fn.name}")
                        if not self.import_diff:
                            return None
                        # tests that run it after import (classes defined inside tests)
                        out |= m.lines(path, own)
                        self.defer(f"import-time code in {outer.name}", None, notes)
                        continue
                    # A factory: what it builds lives on in the functions it returns.
                    out |= m.func_range(path, outer.lineno, outer.end_lineno)
                    notes.append(f"factory {outer.name}")
                    # Having a nested lambda doesn't make it a factory: an ordinary method
                    # that runs at import (the parser, from a generator's class body) can
                    # raise there or bake a different result into a class.
                    if not self.import_diff:
                        return None
                    self.defer(f"import-time code in {outer.name}", None, notes)
                    continue
                out |= m.lines(path, own)
                notes.append(f"body {fn.name}")
                continue
            st = chain[-1]
            qual = [s.name for s in chain if isinstance(s, ast.ClassDef)]
            if isinstance(st, ast.ClassDef):
                if st.body and l >= st.body[0].lineno:
                    continue  # blank line or comment inside the class
                if l < st.lineno and st.decorator_list:
                    notes.append("class decorator")
                    if not self.import_diff:
                        return None
                    self.defer(f"class decorator {st.name}", None, notes)
                    continue
                notes.append(f"class header {st.name}")
                if not self.import_diff:
                    return None
                self.defer(f"class header {st.name}", None, notes)
                continue
            lams = [n for n in ast.walk(st) if isinstance(n, ast.Lambda) and n.body.lineno <= l <= n.end_lineno]
            if lams:
                lam = min(lams, key=lambda n: n.end_lineno - n.lineno)
                out |= m.lines(path, range(lam.lineno, lam.end_lineno + 1))
                notes.append("lambda")
                continue
            names = targets_of(st)
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                notes.append("import line")
                if not self.import_diff:
                    return None
                self.defer(f"import line {l}", None, notes)
                continue
            if not names:
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                    continue  # docstring
                notes.append(f"import-time statement at line {l}")
                if not self.import_diff:
                    return None
                self.defer(f"import-time statement {path}:{l}", None, notes)
                continue
            if qual:
                out |= self.class_attr(f"{modname}:{'.'.join(qual)}", names, notes, constant=l == swap)
            else:
                out |= self.module_global(path, names, notes)
        return out

    def _constant_swap(self, old, new, newt, chain, l, ck):
        """Whether the file's whole change is line l, a class-level `NAME = constant`
        whose constant changed, on a setting the pinned import leaves as plain data."""
        if self.plain_setting is None or not all(isinstance(s, ast.ClassDef) for s in chain[:-1]):
            return False
        ol, nl = old.splitlines(), new.splitlines()
        if len(ol) != len(nl) or ol[:l - 1] != nl[:l - 1] or ol[l:] != nl[l:]:
            return False
        after = stmt_chain(newt, l)
        if len(after) != len(chain) or any(type(a) is not type(b) or getattr(a, "name", None) != getattr(b, "name", None)
                                           for a, b in zip(chain, after)):
            return False

        def scalar_assign(st):
            if not (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                    and st.lineno == st.end_lineno == l):
                return None
            v = st.value
            if isinstance(v, ast.UnaryOp) and isinstance(v.op, (ast.USub, ast.UAdd)):
                v = v.operand
            if not (isinstance(v, ast.Constant) and isinstance(v.value, (bool, int, float, str, type(None)))):
                return None
            return st.targets[0].id

        name = scalar_assign(chain[-1])
        return name is not None and scalar_assign(after[-1]) == name and bool(self.plain_setting(ck, name))

    def analyze(self, patch_text, files):
        """Candidate test indexes for one patch, or None for 'all'."""
        self.deferred = None
        notes = []
        out = set()
        m = self.m
        for fc in files:
            path = fc.path
            if path.startswith("tests/fixtures/") or (path.startswith("tests/") and not path.endswith(".py")):
                s = m.open2t.get(path)
                if not s:
                    notes.append(f"unmapped data file {path}")
                    return None, notes
                notes.append(f"data file {path}: {len(s)}")
                if path in TPC_SQL:
                    s = self.tpc_sql(fc, path, s, notes)
                out |= s
            elif path.startswith("tests/") and path.endswith(".py"):
                s = m.tests_by_file.get(path)
                if s is None:
                    notes.append(f"test helper {path}")
                    return None, notes
                s = self.test_file(fc, path, s, notes)
                if s is None:
                    return None, notes
                out |= s
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                try:
                    r = self.python_file(fc, notes)
                except Exception as e:  # never crash on an odd patch
                    notes.append(f"analysis error {e!r}")
                    r = None
                if r is None:
                    return None, notes
                out |= r
            elif path.endswith(".py"):
                notes.append(f"python outside sqlglot {path}")
                return None, notes
            else:
                notes.append(f"ignored {path}")
        return out, notes


    # ---- import diff ------------------------------------------------------
    def diff_tests(self, before, after, notes):
        """Tests that can see what differs between two importstate snapshots, or
        None when something differs that the map can't attribute to tests."""
        self.import_diff, saved = False, self.import_diff
        try:
            return self._diff_tests(before, after, notes)
        finally:
            self.import_diff = saved

    def _diff_tests(self, before, after, notes):
        m = self.m
        bsnap, asnap = before["snap"], after["snap"]
        out = set()
        failed = set(after["fail"]) - set(before["fail"])
        if failed:
            notes.append(f"import fails: {sorted(failed)[:3]}")
            s = self.failed_import_users(failed, notes)
            if s is None:
                return None
            out |= s
        unstable = before.get("unstable", set())
        classes_before = {k[1] for k in bsnap if k[0] == "a"}
        if not hasattr(m, "_bases"):
            bases = {}
            for b, subs in m.subclasses.items():
                for c in subs:
                    bases.setdefault(c, set()).add(b)
            m._bases = bases
        forced = self.deferred["attrs"] if self.deferred else set()
        changed = []
        balias, aalias = before.get("alias", {}), after.get("alias", {})
        realiased = {k for k in balias.keys() | aalias.keys() if balias.get(k) != aalias.get(k)}
        for k in bsnap.keys() | asnap.keys():
            if failed and (k[1].split(":")[0] in failed):
                continue  # a module that no longer imports: failed_import_users covers it
            b, a = bsnap.get(k), asnap.get(k)
            if k in realiased:
                changed.append(k)  # same contents, but now shared with another setting or no longer
                continue
            if b == a or k in unstable:
                continue
            if failed and a is None and k[0] == "g" and self._import_order_only(k, b, failed, asnap):
                continue
            if b and a and b[0].endswith("!opaque") and a[0].endswith("!opaque"):
                if k[0] == "a" and f"{k[1]}.{k[2]}" in forced:
                    changed.append(k)  # an edited setting we can't compare: keep its readers
                continue
            changed.append(k)
        def users(ck):
            """Tests that read any of the class's settings, or ran a function that
            names the class (instances only come from code that names it)."""
            u = set(m.class_usage(ck))
            for part in {ck.split(":")[1].split(".")[-1], ck.split(":")[1].split(".")[0]}:
                for e in m.names.get(part, ()):
                    if e[0] == "func":
                        u |= m.func_range(e[1], e[2], e[4])
            return u if u else None
        for k in sorted(changed):
            b, a = bsnap.get(k), asnap.get(k)
            kinds = {e[1] for e in (b, a) if e}
            ownfn = all(e[2] for e in (b, a) if e) and b is not None and a is not None
            if k[0] == "a":
                _, ck, name = k
                if ck not in classes_before:
                    continue  # a new class: anything that refers to it differs too
                if kinds == {"plain"}:
                    s = set(m.attr2t.get(f"{ck}.{name}", ()))
                    for sc in m.subclasses.get(ck, ()):
                        s |= m.attr2t.get(f"{sc}.{name}", set())
                    narrowed = None
                    if b is not None and a is not None and k not in realiased:
                        narrowed = self.dict_key_readers(k, before, after, notes)
                    if narrowed is not None:
                        # tests the key map never recorded keep their attribute-level read
                        s = narrowed | (s - self.keyreads.recorded)
                    if b is None or a is None:
                        # it now shadows (or stops shadowing) an inherited setting,
                        # whose reads were recorded on the base class
                        u = users(ck)
                        if u is None:
                            notes.append(f"import diff: no users of {ck}")
                            return None
                        for base in m._bases.get(ck, ()):
                            s |= m.attr2t.get(f"{base}.{name}", set()) & u
                    notes.append(f"import diff {ck}.{name}: {len(s)}")
                    out |= s
                    continue
                if kinds == {"callable"} and ownfn:
                    continue  # a method's own def/body change: the line rules cover it
                if kinds == {"callable"} and b is not None and b[2] and a is None:
                    # a method written here was deleted: everything that called it ran
                    # its lines, which the line rules already map
                    continue
                if "enum" in kinds:
                    # a new member, or the enum's own bookkeeping (_member_names_,
                    # _hashable_values_, ...) that lists it
                    new = b is None or (name.startswith("_") and name.endswith("_") and a is not None)
                    s = self.enum_users(ck) if kinds == {"enum"} and new else None
                    if s is None:
                        notes.append(f"import diff: enum {ck}.{name}")
                        return None
                    # a new member: only code that uses the enum as a whole can see it
                    # (other members keep their names; a shifted IntEnum value changed
                    # no test when every TokenType moved up by 3)
                    notes.append(f"import diff: new enum member {ck}.{name}: {len(s)}")
                    out |= s
                    continue
                u = users(ck)
                if u is None:
                    notes.append(f"import diff: {ck}.{name} changed, class has no recorded users")
                    return None
                notes.append(f"import diff {ck}.{name} ({'/'.join(sorted(kinds))}): class users {len(u)}")
                out |= u
            else:
                _, mod, name = k
                if kinds == {"callable"} and ownfn:
                    continue
                path = mod.replace(".", "/") + ".py"
                if not os.path.exists(os.path.join(m.src, path)):
                    path = mod.replace(".", "/") + "/__init__.py"
                # a module bound by `import x` (added or dropped) is seen only by
                # code in this file, unless another module star-imports this one
                own = kinds == {"module"} and not self._star_imported(mod)
                s = self.module_global(path, {name}, notes, own_file=own, funcs_only=True)
                out |= s
        notes.append(f"import diff: {len(changed)} changed, {len(out)} tests")
        return out

    def failed_import_users(self, failed, notes):
        """Tests that can reach a module the patch makes fail to import (sqlglot imports
        dialect modules lazily, so only code that loads the module sees the error): users
        of its classes, tests running its lines, and test files that import it by name.
        None for a module the map can't place (sqlglot itself, a package __init__)."""
        m = self.m
        out = set()
        for mod in sorted(failed):
            path = mod.replace(".", "/") + ".py"
            table = m.line2t.get(path)
            if mod == "sqlglot" or not mod.startswith(DIALECT_DIRS) or table is None:
                notes.append(f"import fails in {mod}: all tests")
                return None
            for c in m.cls2t:
                if c.startswith(mod + ":"):
                    out |= m.class_usage(c)
            for s in table.values():
                out |= s
            # `from sqlglot.dialects.duckdb import X`, `from sqlglot.dialects import DuckDB`
            names = {c.split(":")[1].split(".")[0] for c in m.cls2t if c.startswith(mod + ":")}
            names.add(mod.rsplit(".", 1)[1])
            def direct(f):
                for node in self._test_imports(f):
                    if node is None:
                        return None
                    if node[0] == mod or (node[0] or "").startswith(mod + ".") or (
                            mod.startswith((node[0] or "") + ".") and names & node[1]):
                        return True
                return False
            # through test helpers too: test_spark.py imports tests/dialects/test_dialect.py,
            # which imports sqlglot.dialects.duckdb
            hit = {}
            def reaches(f, seen):
                if f in hit:
                    return hit[f]
                r = direct(f)
                if r is False:
                    for g in self._test_deps(f):
                        if g not in seen:
                            seen.add(g)
                            r2 = reaches(g, seen)
                            if r2 is not False:
                                r = r2
                                break
                hit[f] = r
                return r
            for f, ts in m.tests_by_file.items():
                r = reaches(f, {f})
                if r is None:
                    return None
                if r:
                    out |= ts
        notes.append(f"users of modules that fail to import: {len(out)}")
        return out

    def _import_order_only(self, k, b, failed, asnap):
        """A package global that is gone only because a module failed to import: the
        package's binding of that submodule, or a name a lazy package __getattr__ had
        cached while the module loaded (sqlglot.dialects.Dialect), whose value is still
        there, unchanged, in the module it comes from."""
        _, mod, name = k
        if f"{mod}.{name}" in failed:
            return True
        path = os.path.join(self.m.src, mod.replace(".", "/"), "__init__.py")
        lazy = self.__dict__.setdefault("_lazy", {})
        if mod not in lazy:
            try:
                with open(path, encoding="utf-8") as f:
                    lazy[mod] = "\ndef __getattr__(" in f.read()
            except OSError:
                lazy[mod] = False
        if not lazy[mod] or not b[0].endswith(tuple("0123456789abcdef")):
            return False
        if getattr(self, "_after_vals", (None,))[0] is not asnap:
            where = {}
            for kk, v in asnap.items():
                if kk[0] == "g":
                    where.setdefault((v[0], v[1]), set()).add(kk[1])
            self._after_vals = (asnap, where)
        return bool(self._after_vals[1].get((b[0], b[1]), set()) - {mod})

    def _test_deps(self, f):
        """Files under tests/ that test file (or helper) f imports."""
        out = []
        d = os.path.dirname(f)
        while d.startswith("tests"):  # the packages it sits in are imported first
            if os.path.exists(os.path.join(self.m.src, d, "__init__.py")):
                out.append(f"{d}/__init__.py")
            d = os.path.dirname(d)
        for node in self._test_imports(f):
            if node is None:
                continue
            mod, names = node
            if not mod or not (mod == "tests" or mod.startswith("tests.")):
                continue
            base = mod.replace(".", "/")
            for cand in [base + ".py", base + "/__init__.py"] + [f"{base}/{n}.py" for n in names]:
                if os.path.exists(os.path.join(self.m.src, cand)):
                    out.append(cand)
        return out

    def _test_imports(self, f):
        """(module, imported names) for each import in test file f; [None] if unreadable."""
        cache = self.__dict__.setdefault("_timports", {})
        if f not in cache:
            try:
                with open(os.path.join(self.m.src, f), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError, ValueError):
                cache[f] = [None]
                return cache[f]
            r = []
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.level == 0:
                    r.append((n.module, {a.name for a in n.names}))
                elif isinstance(n, ast.Import):
                    r += [(a.name, set()) for a in n.names]
            cache[f] = r
        return cache[f]

    def enum_users(self, ck):
        """Tests running a function that uses enum class ck (or a plain alias of it,
        like DataType.Type = DType) as a whole: iterating it, X[name], X(value),
        getattr(X, name), ... (build_map.whole_mentions). None without that index."""
        m = self.m
        wnames = getattr(m, "wnames", None)
        if wnames is None:
            return None
        cname = ck.split(":")[1].split(".")[-1]
        alias_of = wnames.get("<aliases>", {})
        aliases, work = {cname}, [cname]
        while work:
            for a in alias_of.get(work.pop(), ()):
                if a not in aliases:
                    aliases.add(a)
                    work.append(a)
        funcs = {}
        for es in (m.names.get(a, ()) for a in aliases):
            for e in es:
                if e[0] == "func":
                    funcs[(e[1], e[2])] = e
        out = set()
        for a in aliases:
            for key in wnames.get(a, ()):
                e = funcs.get(key)
                if e is None:
                    return None
                out |= m.func_range(e[1], e[2], e[4])
        return out

    def dict_key_readers(self, k, before, after, notes):
        """Tests that read a key a patch changed in the dict setting k (and the same
        setting on subclasses), or None when that can't be said at key level."""
        kr = self.keyreads
        _, ck, name = k
        bd, ad = before.get("dicts", {}).get(k), after.get("dicts", {}).get(k)
        if kr is None or bd is None or ad is None or not kr.covers(f"{ck}.{name}"):
            return None
        changed, reordered = dict_change(bd, ad)
        out = set()
        for attr in [f"{ck}.{name}"] + [f"{sc}.{name}" for sc in self.m.subclasses.get(ck, ())]:
            if kr.covers(attr):
                r = kr.readers(attr, changed, reordered)
                if r is None:
                    return None
                out |= r
            else:
                out |= self.m.attr2t.get(attr, set())
        notes.append(f"keys of {ck}.{name}: {sorted(map(str, changed))[:3]}{' reordered' if reordered else ''} -> {len(out)} tests")
        return out
