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


class Maps:
    def __init__(self, path, src):
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
    def __init__(self, maps, import_diff=False, keyreads=None):
        self.m = maps
        self.keyreads = keyreads
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
    def class_attr(self, ck, names, notes):
        m = self.m
        keys = {f"{ck}.{n}" for n in names}
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
    def module_global(self, path, names, notes, depth=0, own_file=False):
        """own_file: only code in this module can see the name (a module object
        bound by `import x`: nothing reaches it as othermodule.x)."""
        m = self.m
        out = set()
        modname = path[:-3].replace("/", ".").removesuffix(".__init__")
        for n in names:
            for e in m.names.get(n, ()):
                if own_file and e[1] != (modname if e[0] == "classbody" else path):
                    continue
                if e[0] == "func":
                    out |= m.func_range(e[1], e[2], e[4])
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
        for l in sorted(changed):
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
                out |= self.class_attr(f"{modname}:{'.'.join(qual)}", names, notes)
            else:
                out |= self.module_global(path, names, notes)
        return out

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
        if set(after["fail"]) - set(before["fail"]):
            notes.append(f"import fails: {sorted(set(after['fail']) - set(before['fail']))[:3]}")
            return None
        unstable = before.get("unstable", set())
        classes_before = {k[1] for k in bsnap if k[0] == "a"}
        if not hasattr(m, "_bases"):
            bases = {}
            for b, subs in m.subclasses.items():
                for c in subs:
                    bases.setdefault(c, set()).add(b)
            m._bases = bases
        out = set()
        forced = self.deferred["attrs"] if self.deferred else set()
        changed = []
        balias, aalias = before.get("alias", {}), after.get("alias", {})
        realiased = {k for k in balias.keys() | aalias.keys() if balias.get(k) != aalias.get(k)}
        for k in bsnap.keys() | asnap.keys():
            b, a = bsnap.get(k), asnap.get(k)
            if k in realiased:
                changed.append(k)  # same contents, but now shared with another setting or no longer
                continue
            if b == a or k in unstable:
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
                    notes.append(f"import diff: enum {ck}.{name}")
                    return None
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
                s = self.module_global(path, {name}, notes, own_file=own)
                out |= s
        notes.append(f"import diff: {len(changed)} changed, {len(out)} tests")
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
