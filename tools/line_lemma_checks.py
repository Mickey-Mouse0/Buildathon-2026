"""Hand-made cases for blast/linelemma.py: edits it must accept (answer: the line) and
edits it must refuse (answer: None). Run: python tools/line_lemma_checks.py"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blast"))

import linelemma  # noqa: E402

BASE = '''\
from __future__ import annotations
import typing as t

LIMIT = 3


class K:
    FLAG = True

    def cond(self, a, b, c):
        if (
            a.ready
            and b.kind == "x"
            and c in (1,
                      2,
                      3)
        ):
            return True
        return False

    def gen(self, xs):
        for x in xs:
            if x > LIMIT:
                yield x

    def one(self, x): return x == 1

    def default(self, x=1):
        y = x + 1
        return y

    def lam(self):
        f = lambda v: (
            v + 1
        )
        return f

    def compr(self, xs):
        return [
            x
            for x in xs
            if x != 0
        ]

    def tryit(self, x):
        try:
            return x.a
        except KeyError:
            return x.b == 2

    def is_none(self, x):
        if x is None:
            return 0
        return 1


def top(a, b):
    if a and b:
        return 1
    return 2
'''

CASES = [
    # (name, old line text, new line text, expected: line number or None)
    ("compare on its own operand line", 'and b.kind == "x"', 'and b.kind != "x"', "line"),
    ("and/or flip joining operands on other lines", 'and b.kind == "x"', 'or b.kind == "x"', None),
    ("constant folded into a tuple that starts on another line", "                      2,", "                      5,", None),
    ("inside a generator", "if x > LIMIT:", "if x >= LIMIT:", None),
    ("function written on one line", "def one(self, x): return x == 1", "def one(self, x): return x != 1", None),
    ("a default value", "def default(self, x=1):", "def default(self, x=2):", None),
    ("constant in a plain statement", "y = x + 1", "y = x + 2", "line"),
    ("body of a multi-line lambda", "v + 1", "v + 2", "line"),
    ("comprehension condition (inlined in 3.13)", "if x != 0", "if x == 0", "line"),
    ("inside an except block", "return x.b == 2", "return x.b != 2", "line"),
    ("class setting", "FLAG = True", "FLAG = False", None),
    ("module constant", "LIMIT = 3", "LIMIT = 4", None),
    ("is None to == None (other instructions)", "if x is None:", "if x == None:", None),
    ("and/or on one line", "if a and b:", "if a or b:", "line"),
    ("return constant", "return 1\n", "return 3\n", "line"),
    ("comment only", "return 2", "return 2  # two", None),
    ("docstring-like no code change", "y = x + 1", "y = x + 1 ", None),
]


def main():
    bad = 0
    for name, old_line, new_line, want in CASES:
        assert BASE.count(old_line) >= 1, name
        new = BASE.replace(old_line, new_line, 1)
        line = next(i for i, (x, y) in enumerate(zip(BASE.split("\n"), new.split("\n")), 1) if x != y)
        got = linelemma.one_line("toy.py", ast.parse(BASE), ast.parse(new), BASE.split("\n"), new.split("\n"))
        expect = line if want == "line" else None
        ok = got == expect
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: line {line}, lemma says {got}, expected {expect}")
    # two lines changed, a line added: never
    two = BASE.replace("y = x + 1", "y = x + 2").replace("return 2", "return 3")
    got = linelemma.one_line("toy.py", ast.parse(BASE), ast.parse(two), BASE.split("\n"), two.split("\n"))
    print(f"{'ok  ' if got is None else 'FAIL'} two lines changed: {got}")
    bad += got is not None
    print(f"{len(CASES) + 1 - bad} of {len(CASES) + 1} pass")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
