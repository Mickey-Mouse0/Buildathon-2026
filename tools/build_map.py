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
                    elif isinstance(st, ast.ClassDef):
                        visit(st.body, qual + [st.name], True)
                    else:
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
    return dict(idx)


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
        "names": name_index(src),
    }
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"map: {len(ids)} tests, {sum(len(m) for m in line2t.values())} lines, "
          f"{len(attr2t)} attributes, {os.path.getsize(out) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
