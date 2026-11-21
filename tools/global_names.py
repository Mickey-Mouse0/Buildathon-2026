"""Add the static name indexes (build_map.global_mentions, whole_mentions) to a built map.

    python tools/global_names.py <sqlglot dir> <covmap.pkl.gz>

It is static (source only), so a map built before it existed gets it without a rerun.
"""
import gzip
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_map import name_index  # noqa: E402


def main():
    src, path = sys.argv[1:3]
    with gzip.open(path, "rb") as f:
        d = pickle.load(f)
    names, gnames, wnames = name_index(src)
    assert names == d["names"], "map was built from another source tree"
    d["gnames"], d["wnames"] = gnames, wnames
    with gzip.open(path, "wb", compresslevel=9) as f:
        pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"gnames: {len(gnames)} names, wnames: {len(wnames)}", file=sys.stderr)


if __name__ == "__main__":
    main()
