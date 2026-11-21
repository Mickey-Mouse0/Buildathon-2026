"""Turn the raw per-worker coverage files into the compact map the selector loads.

    python tools/build_map.py <raw_dir> <sqlglot_src> <out.pkl.gz>

Everything here comes from running sqlglot's own tests at the pinned commit; no
patch is looked at.
"""
import ast
import glob
import gzip
import json
import os
import pickle
import sys
from collections import defaultdict


def merge(raw_dir):
    tests, opened, attrs, imp, mro = {}, {}, {}, defaultdict(set), {}
    for path in glob.glob(os.path.join(raw_dir, "*.json")):
        with open(path) as f:
            d = json.load(f)
        for t, v in d["tests"].items():
            cur = tests.setdefault(t, defaultdict(set))
            for fn, ls in v.items():
                cur[fn].update(ls)
        for t, v in d["opened"].items():
            opened.setdefault(t, set()).update(v)
        for t, v in d.get("attrs", {}).items():
            attrs.setdefault(t, set()).update(v)
        for fn, ls in d["import"].items():
            imp[fn].update(ls)
        mro.update(d.get("mro", {}))
    return tests, opened, attrs, imp, mro


def refs_of(node):
    """Names a class-body statement's value reads: (None, name) for bare names,
    (base, attr) for attribute reads."""
    out = set()
    if node is None:
        return out
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            out.add((None, x.id))
        elif isinstance(x, ast.Attribute):
            b = x.value
            bn = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None
            out.add((bn, x.attr))
    return out


def targets_of(st):
    if isinstance(st, ast.Assign):
        tg = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        tg = [st.target]
    else:
        return set()
    return {x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)}


def name_index(src):
    """identifier -> entries, built statically from the source:
    ("func", path, start, body_start, end): a function that mentions the identifier
    ("classbody", module, qualname, targets, refs): a class-body statement that mentions it
    ("module", path, targets): a module-level statement that mentions it
    """
    idx = defaultdict(list)
    gidx = defaultdict(set)
    widx = defaultdict(set)
    aidx = defaultdict(set)
    # tests/ too: a test can read a module constant directly (test_typing reads
    # every typing module's EXPRESSION_METADATA through importlib)
    walk = [w for top in ("sqlglot", "tests") for w in os.walk(os.path.join(src, top))]
    for dp, _, fns in walk:
        if "fixtures" in dp.split(os.sep):
            continue
        for fn in sorted(fns):
            if not fn.endswith(".py"):
                continue
            path = os.path.relpath(os.path.join(dp, fn), src)
            with open(os.path.join(src, path)) as f:
                tree = ast.parse(f.read())
            modname = path[:-3].replace("/", ".").removesuffix(".__init__")
            imported = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree)
                        if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}

            def visit(body, qual, inclass):
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names = set()
                        for n in ast.walk(st):
                            if isinstance(n, ast.Name):
                                names.add(n.id)
                            elif isinstance(n, ast.Attribute):
                                names.add(n.attr)
                            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                                names.add(n.value)
                        start = st.decorator_list[0].lineno if st.decorator_list else st.lineno
                        entry = ("func", path, start, st.body[0].lineno, st.end_lineno)
                        for nm in names:
                            idx[nm].append(entry)
                        for nm in global_mentions(st, imported):
                            gidx[nm].add((path, start))
                        for nm in whole_mentions(st):
                            widx[nm].add((path, start))
                    elif isinstance(st, ast.ClassDef):
                        visit(st.body, qual + [st.name], True)
                    else:
                        for a, b in aliases_of(st):
                            aidx[a].add(b)
                        val = getattr(st, "value", None)
                        refs = frozenset(refs_of(val)) if val is not None else frozenset(refs_of(st))
                        names = {n for _, n in refs}
                        if inclass:
                            entry = ("classbody", modname, ".".join(qual), frozenset(targets_of(st)), refs)
                        else:
                            entry = ("module", path, frozenset(targets_of(st)))
                        for nm in names:
                            idx[nm].append(entry)

            visit(tree.body, [], False)
    wnames = {k: frozenset(v) for k, v in widx.items()}
    wnames["<aliases>"] = {k: frozenset(v) for k, v in aidx.items()}
    return dict(idx), {k: frozenset(v) for k, v in gidx.items()}, wnames


def aliases_of(st):
    """(name, other name) for a module or class statement that binds the same object
    under another name: A = X, A: T = mod.X, from m import X as A."""
    if isinstance(st, ast.ImportFrom):
        return [(a.name, a.asname) for a in st.names if a.asname and a.asname != a.name]
    if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, (ast.Name, ast.Attribute)):
        v = st.value.id if isinstance(st.value, ast.Name) else st.value.attr
        return [(v, t) for t in targets_of(st) if t != v]
    return []


def whole_mentions(fn):
    """Names a function uses as a whole object rather than to reach a member: every
    mention except X.member (a public attribute) and type annotations. For an enum
    class, these are the places that can see a member being added: iterating it,
    len(), X[name], X(value), getattr(X, name), X.__members__."""
    parent = {}
    for n in ast.walk(fn):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
    skip = set()
    for n in ast.walk(fn):
        anns = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            anns += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                     if x is not None and x.annotation is not None]
            if n.returns is not None:
                anns.append(n.returns)
        elif isinstance(n, ast.AnnAssign):
            anns.append(n.annotation)
        for an in anns:
            skip |= set(ast.walk(an))
    out = set()
    for n in ast.walk(fn):
        if n in skip:
            continue
        if isinstance(n, ast.Name):
            nm = n.id
        elif isinstance(n, ast.Attribute):
            nm = n.attr
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
            continue
        else:
            continue
        p = parent.get(n)
        if isinstance(p, ast.Attribute) and p.value is n and not p.attr.startswith("_"):
            continue  # X.MEMBER, X.method
        out.add(nm)
    return out


def global_mentions(fn, imported=frozenset()):
    """Names a function mentions in a way that could read a module global: a bare
    name, a string (getattr, importlib), or an attribute of anything except self,
    cls, a parameter or a local that plain assignments bind to a non-module value
    (dialect.EXPRESSION_METADATA reads the Dialect class setting, which the import
    diff tracks, not the typing module's dict of that name). A name that is also
    imported, or bound any other way (for, with, global, ...), still counts."""
    ok, bad = {"self", "cls"}, set(imported)
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = n.args
            ok |= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            ok |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            plain = n.value is not None and not _module_like(n.value)
            for t in tg:
                if isinstance(t, ast.Name):
                    (ok if plain else bad).add(t.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bad |= set(n.names)
    for n in ast.walk(fn):
        for t in _other_targets(n):
            bad.add(t)
    local = ok - bad
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
        elif isinstance(n, ast.Attribute):
            root = n.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in local):
                out.add(n.attr)
    return out


def _module_like(v):
    """Could this expression evaluate to a module object?"""
    for x in ast.walk(v):
        if isinstance(x, ast.Name) and x.id in ("__import__", "import_module", "reload", "modules", "getattr", "vars"):
            return True
        if isinstance(x, ast.Attribute) and x.attr in ("import_module", "reload", "modules", "__dict__"):
            return True
    return isinstance(v, ast.Name)  # an alias of another name, e.g. m = some_module


def _other_targets(n):
    """Names bound by anything but a plain assignment or a parameter."""
    tg = []
    if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
        tg.append(n.target)
    elif isinstance(n, (ast.With, ast.AsyncWith)):
        tg += [i.optional_vars for i in n.items if i.optional_vars is not None]
    elif isinstance(n, ast.NamedExpr):
        tg.append(n.target)
    elif isinstance(n, ast.AugAssign):
        tg.append(n.target)
    elif isinstance(n, ast.ExceptHandler) and n.name:
        return [n.name]
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in n.names]
    elif isinstance(n, ast.Assign):
        tg += [t for t in n.targets if not isinstance(t, ast.Name)]  # unpacking
    elif isinstance(n, ast.match_case):
        return [x.name for x in ast.walk(n.pattern) if getattr(x, "name", None)]
    return [x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)]



def main(raw_dir, src, out):
    tests, opened, attrs, imp, mro = merge(raw_dir)
    ids = sorted(tests)
    num = {t: i for i, t in enumerate(ids)}
    pool = {}

    def intern(s):
        fs = frozenset(s)
        return pool.setdefault(fs, fs)

    line_sets = defaultdict(lambda: defaultdict(set))
    for t, d in tests.items():
        for fn, ls in d.items():
            m = line_sets[fn]
            for l in ls:
                m[l].add(num[t])
    line2t = {fn: {l: intern(s) for l, s in m.items()} for fn, m in line_sets.items()}

    open2t = defaultdict(set)
    for t, fs in opened.items():
        for f in fs:
            open2t[f].add(num[t])
    attr2t, cls2t = defaultdict(set), defaultdict(set)
    for t, ks in attrs.items():
        for k in ks:
            attr2t[k].add(num[t])
            cls2t[k.rsplit(".", 1)[0]].add(num[t])
    subclasses = defaultdict(set)
    for c, bases in mro.items():
        for b in bases:
            subclasses[b].add(c)
    by_file = defaultdict(set)
    for t in ids:
        by_file[t.split("::")[0]].add(num[t])

    data = {
        "tests": ids,
        "line2t": line2t,
        "importtime": {f: frozenset(v) for f, v in imp.items()},
        "open2t": {f: intern(v) for f, v in open2t.items()},
        "attr2t": {k: intern(v) for k, v in attr2t.items()},
        "cls2t": {k: intern(v) for k, v in cls2t.items()},
        "subclasses": {k: tuple(sorted(v)) for k, v in subclasses.items()},
        "tests_by_file": {k: frozenset(v) for k, v in by_file.items()},
    }
    data["names"], data["gnames"], data["wnames"] = name_index(src)
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"map: {len(ids)} tests, {sum(len(m) for m in line2t.values())} lines, "
          f"{len(attr2t)} attributes, {os.path.getsize(out) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
