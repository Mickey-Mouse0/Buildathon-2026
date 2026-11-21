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

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
DIALECT_DIRS = ("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.",
                "sqlglot.tokenizers.", "sqlglot.typing.")


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
    def __init__(self, maps):
        self.m = maps

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
            out |= u
        return out

    # ---- module constants -----------------------------------------------
    def module_global(self, path, names, notes, depth=0):
        m = self.m
        out = set()
        for n in names:
            for e in m.names.get(n, ()):
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
        if ast.dump(oldt) == ast.dump(newt):
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
                        return None
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
                    return None
                notes.append(f"class header {st.name}")
                return None
            lams = [n for n in ast.walk(st) if isinstance(n, ast.Lambda) and n.body.lineno <= l <= n.end_lineno]
            if lams:
                lam = min(lams, key=lambda n: n.end_lineno - n.lineno)
                out |= m.lines(path, range(lam.lineno, lam.end_lineno + 1))
                notes.append("lambda")
                continue
            names = targets_of(st)
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                notes.append("import line")
                return None
            if not names:
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                    continue  # docstring
                notes.append(f"import-time statement at line {l}")
                return None
            if qual:
                out |= self.class_attr(f"{modname}:{'.'.join(qual)}", names, notes)
            else:
                out |= self.module_global(path, names, notes)
        return out

    def analyze(self, patch_text, files):
        """Candidate test indexes for one patch, or None for 'all'."""
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
                out |= s
            elif path.startswith("tests/") and path.endswith(".py"):
                s = m.tests_by_file.get(path)
                if s is None:
                    notes.append(f"test helper {path}")
                    return None, notes
                notes.append(f"test file {path}: {len(s)}")
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
