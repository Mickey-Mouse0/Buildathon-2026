"""Add one-line functions (lambdas) to a map recorded before the plugin saw them.

    python tools/lambda_lines.py <raw dir> <covmap.pkl.gz>

CPython raises no LINE event for code that sits on a single line, such as
`"COALESCE": lambda *args: ...` in a table, so the map only ever saw those lines run
at import and credited them to no test. A change there then selected nothing. The
coverage plugin now records them on PY_START. This reads a run made with
COVMAP_PYSTART_ONLY=1 COVMAP_ATTRS=0 (tools/run_coverage.sh with those set, minus the
build step) and unions its lines into line2t, so it only ever adds tests.
"""
import glob
import gzip
import json
import os
import pickle
import sys


def main():
    raw, path = sys.argv[1:3]
    with gzip.open(path, "rb") as f:
        d = pickle.load(f)
    index = {t: i for i, t in enumerate(d["tests"])}
    add = {}
    unknown = set()
    for fn in glob.glob(os.path.join(raw, "*.json")):
        with open(fn) as f:
            run = json.load(f)
        for t, files in run["tests"].items():
            i = index.get(t)
            if i is None:
                unknown.add(t)
                continue
            for f_, ls in files.items():
                for l in ls:
                    add.setdefault(f_, {}).setdefault(l, set()).add(i)
    added = 0
    line2t = d["line2t"]
    for f_, m in add.items():
        cur = dict(line2t.get(f_, {}))
        for l, s in m.items():
            old = cur.get(l, frozenset())
            new = old | s
            added += len(new) - len(old)
            cur[l] = frozenset(new)
        line2t[f_] = cur
    with gzip.open(path, "wb", compresslevel=9) as f:
        pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"added {added} (line, test) pairs over {sum(map(len, add.values()))} lines; "
          f"{len(unknown)} tests not in the map", file=sys.stderr)


if __name__ == "__main__":
    main()
