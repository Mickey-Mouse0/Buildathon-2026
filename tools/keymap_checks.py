"""Hand-made checks of key-level reads: every toy test whose result changes after a
dict edit must be selected.

    python tools/keymap_checks.py

Each toy test reads a dict setting some way (a key computed from another key's value,
a miss, iteration, len, copies, **kwargs, formatting, json, ...). We record its reads
through KeyLog, apply an edit, and compare what the key map selects with which tests
really return something different.
"""
import copy
import enum
import gzip
import json
import os
import pickle
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "blast"))

import keymap_plugin as kp  # noqa: E402
from importstate import _Canon, dict_info  # noqa: E402
from keyreads import KeyReads, dict_change  # noqa: E402


class Color(enum.Enum):
    RED = 1
    BLUE = 2


class Marker:
    pass


BASE = {"a": "b", "b": 10, "c": 20, 1: "one", Color.RED: "r", Marker: "cls", "n": {"inner": 1}, "s": "x"}

TESTS = {
    "chained lookup D[D['a']]": lambda D: D[D["a"]],
    "miss with get": lambda D: D.get("z", "none"),
    "miss with in": lambda D: "z" in D,
    "iterate keys": lambda D: list(D),
    "len": lambda D: len(D),
    "truthiness": lambda D: bool(D),
    "items": lambda D: sorted(map(repr, D.items())),
    "values": lambda D: sorted(map(repr, D.values())),
    "lookup with True (== 1)": lambda D: D.get(True),
    "lookup with 1.0": lambda D: D.get(1.0),
    "copy then read": lambda D: {**D}["c"],
    "dict() then read": lambda D: dict(D)["c"],
    "kwargs": lambda D: (lambda **kw: kw.get("s"))(**{k: v for k, v in D.items() if isinstance(k, str)}),
    "percent format": lambda D: "%(s)s" % D,
    "format_map": lambda D: "{s}".format_map(D),
    "union": lambda D: (D | {})["b"],
    "json": lambda D: json.dumps({k: v for k, v in D.items() if isinstance(k, str)}, sort_keys=True),
    "deepcopy": lambda D: copy.deepcopy(D)["b"],
    "enum key": lambda D: D.get(Color.RED),
    "class key": lambda D: D.get(Marker),
    "nested value": lambda D: D["n"]["inner"],
    "equality": lambda D: D == BASE,
    "reversed": lambda D: list(reversed(D)),
    "setdefault": lambda D: D.setdefault("q", 5),
}

EDITS = {
    "change a": lambda D: D.__setitem__("a", "c"),
    "change b": lambda D: D.__setitem__("b", 11),
    "change c": lambda D: D.__setitem__("c", 21),
    "add z": lambda D: D.__setitem__("z", 1),
    "add q": lambda D: D.__setitem__("q", 6),
    "change 1": lambda D: D.__setitem__(1, "uno"),
    "change enum key": lambda D: D.__setitem__(Color.RED, "rr"),
    "change class key": lambda D: D.__setitem__(Marker, "k"),
    "change nested": lambda D: D.__setitem__("n", {"inner": 2}),
    "change s": lambda D: D.__setitem__("s", "y"),
    "delete c": lambda D: D.__delitem__("c"),
    "reorder": lambda D: [D.__setitem__(k, D.pop(k)) for k in ["a"]],
}


def outcome(fn, d):
    try:
        return ("ok", repr(fn(d)))
    except Exception as e:
        return ("raised", type(e).__name__)


def main():
    names = list(TESTS)
    kp._groups[0] = [("a", "toy:Toy", "D")]
    reads = {}
    for ti, name in enumerate(names):
        kl = kp.KeyLog(BASE)
        kl._gid = 0
        kp._cur[0] = {}
        TESTS[name](kl)
        for k in kp._norm(kp._cur[0]).get(tuple(kp._groups[0]), ()):
            reads.setdefault(k, []).append(ti)
        kp._cur[0] = None
    path = os.path.join(tempfile.mkdtemp(), "keymap.pkl.gz")
    with gzip.open(path, "wb") as f:
        pickle.dump({"tests": names, "failed": [], "groups": [tuple(kp._groups[0])], "reads": [reads],
                     "collect": [set()], "unsafe": set()}, f)
    kr = KeyReads(path, {n: i for i, n in enumerate(names)})
    failures = 0
    for ename, edit in EDITS.items():
        patched = dict(BASE)
        edit(patched)
        changed, reordered = dict_change(dict_info(_Canon(), BASE), dict_info(_Canon(), patched))
        selected = kr.readers("toy:Toy.D", changed, reordered)
        affected = {i for i, n in enumerate(names) if outcome(TESTS[n], dict(BASE)) != outcome(TESTS[n], dict(patched))}
        missed = affected - selected
        status = "FAIL" if missed else "ok"
        failures += bool(missed)
        print(f"{status:4} {ename:17} changed={sorted(map(str, changed))}{' reordered' if reordered else ''} "
              f"affected={len(affected)} selected={len(selected)}"
              + (f" MISSED {[names[i] for i in sorted(missed)]}" if missed else ""))
    print(f"{failures} failing checks")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
