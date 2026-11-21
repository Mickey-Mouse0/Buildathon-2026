"""Lines of sqlglot that run while it is imported, every module included.

    python tools/import_lines.py <sqlglot dir> [covmap.pkl.gz]

With a map path, adds them to the map's "importtime" table (a union, so it only
ever makes the map more cautious) and rewrites the map in place. This repairs maps
recorded while the coverage plugin missed function bodies run during
`import sqlglot` (metaclass and __init_subclass__ code).
"""
import gzip
import importlib
import os
import pickle
import pkgutil
import sys


def record(src):
    src = os.path.abspath(src)
    pkg = os.path.join(src, "sqlglot") + os.sep
    mon = sys.monitoring
    seen = set()

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(pkg):
            seen.add((fn[len(src) + 1:], line))
        return mon.DISABLE

    mon.use_tool_id(4, "importlines")
    mon.register_callback(4, mon.events.LINE, on_line)
    mon.set_events(4, mon.events.LINE)
    sys.path.insert(0, src)
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception as e:
                print("import fail", m.name, e, file=sys.stderr)
    mon.set_events(4, 0)
    out = {}
    for f, l in seen:
        out.setdefault(f, set()).add(l)
    return out


def main():
    lines = record(sys.argv[1])
    print(f"{sum(map(len, lines.values()))} lines run at import", file=sys.stderr)
    if len(sys.argv) > 2:
        with gzip.open(sys.argv[2], "rb") as f:
            d = pickle.load(f)
        added = 0
        for f, ls in lines.items():
            old = d["importtime"].get(f, frozenset())
            added += len(ls - old)
            d["importtime"][f] = frozenset(old | ls)
        with gzip.open(sys.argv[2], "wb", compresslevel=9) as f:
            pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"added {added} lines to the map's import table", file=sys.stderr)


if __name__ == "__main__":
    main()
