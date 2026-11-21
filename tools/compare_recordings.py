"""Compare two key-map / tokenizer recordings test by test.

    python tools/compare_recordings.py <dir A> <dir B> [tests.json]

Each dir holds keymap.pkl.gz and tokreplay.pkl.xz. With tests.json, only those tests
are compared (for a spot check recorded on a subset). Prints every test whose key
reads, tokenizer calls or outside-the-tokenizer reads differ, and the code ranges
that ran outside the tokenizer in only one recording.
"""
import gzip
import json
import lzma
import pickle
import sys
from pathlib import Path


def keymap_reads(path):
    d = pickle.load(gzip.open(path))
    per = {t: set() for t in d["tests"]}
    for gid, reads in enumerate(d["reads"]):
        g = d["groups"][gid]
        for k, ts in reads.items():
            for ti in ts:
                per[d["tests"][ti]].add((g, k))
    return per, d


def tok(path):
    d = pickle.load(lzma.open(path))
    calls = {t: (None if c is None else {d["inputs"][i] for i in c}) for t, c in zip(d["tests"], d["calls"])}
    reads = {t: set() for t in d["tests"]}
    for a, ts in d["out_reads"].items():
        for ti in ts:
            reads[d["tests"][ti]].add(a)
    outside = {(p, a, b) for p, rs in d["outside"].items() for a, b in rs}
    return calls, reads, outside, d


def show(name, a, b, only):
    keys = (a.keys() | b.keys()) if only is None else only
    bad = 0
    for t in sorted(keys):
        x, y = a.get(t, "missing"), b.get(t, "missing")
        if x != y:
            bad += 1
            if bad <= 8:
                if isinstance(x, set) and isinstance(y, set):
                    print(f"  {name} {t}: only A {sorted(map(str, x - y))[:3]} only B {sorted(map(str, y - x))[:3]}")
                else:
                    print(f"  {name} {t}: A={str(x)[:80]} B={str(y)[:80]}")
    print(f"{name}: {bad} of {len(keys)} tests differ")
    return bad


def main(a, b, tests_json=None):
    only = None
    if tests_json:
        only = {t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}
    ka, _ = keymap_reads(Path(a, "keymap.pkl.gz"))
    kb, _ = keymap_reads(Path(b, "keymap.pkl.gz"))
    total = show("key reads", ka, kb, only)
    ca, ra, oa, _ = tok(Path(a, "tokreplay.pkl.xz"))
    cb, rb, ob, _ = tok(Path(b, "tokreplay.pkl.xz"))
    total += show("tokenizer calls", ca, cb, only)
    total += show("settings read outside the tokenizer", ra, rb, only)
    if only is None:
        print(f"code that ran outside the tokenizer: {len(oa - ob)} ranges only in A, {len(ob - oa)} only in B")
        for r in sorted(oa ^ ob)[:10]:
            print("  ", "A" if r in oa else "B", r)
    print(f"{total} differences in all")


if __name__ == "__main__":
    main(*sys.argv[1:4])
