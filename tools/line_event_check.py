"""Checks what blast/linelemma.py leans on: whenever an instruction of line L runs in a
frame, sys.monitoring has raised a LINE event for L in that code object, unless the
frame started on L. Runs some of sqlglot's tests with LINE and INSTRUCTION events on.

    python tools/line_event_check.py <sqlglot dir> [unittest ids ...]
"""
import os
import sys
import unittest

src = os.path.abspath(sys.argv[1])
sys.path.insert(0, src)
os.chdir(src)
os.environ["SKIP_INTEGRATION"] = "0"
PKG = os.path.join(src, "sqlglot") + os.sep
mon = sys.monitoring
TOOL = 3
lines, instrs = {}, {}


def on_line(code, line):
    if not code.co_filename.startswith(PKG):
        return mon.DISABLE
    lines.setdefault(code, set()).add(line)


def on_instr(code, offset):
    if not code.co_filename.startswith(PKG):
        return mon.DISABLE
    instrs.setdefault(code, set()).add(offset)
    return mon.DISABLE


import sqlglot  # noqa: E402

names = sys.argv[2:] or ["tests.test_optimizer.TestOptimizer.test_simplify", "tests.dialects.test_duckdb",
                         "tests.test_transpile", "tests.test_executor.TestExecutor.test_py_dialect"]
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
mon.use_tool_id(TOOL, "check")
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.register_callback(TOOL, mon.events.INSTRUCTION, on_instr)
mon.set_events(TOOL, mon.events.LINE | mon.events.INSTRUCTION)
res = unittest.TextTestRunner(verbosity=0).run(suite)
mon.set_events(TOOL, 0)

import dis  # noqa: E402

bad = {"entry line": 0, "generator": 0, "other": 0}
checked = 0
examples = []
for code, offs in instrs.items():
    line_of = {}
    entry = None
    for x in dis.get_instructions(code):
        line_of[x.offset] = x.positions.lineno if x.positions else None
        if entry is None and x.opname == "RESUME":
            entry = line_of[x.offset]
    seen = lines.get(code, set())
    for o in offs:
        ln = line_of.get(o)
        if ln is None:
            continue
        checked += 1
        if ln not in seen:
            if ln == entry:
                bad["entry line"] += 1
            elif code.co_flags & (0x20 | 0x80 | 0x200):
                bad["generator"] += 1
                if len(examples) < 5:
                    examples.append((code.co_filename[len(src) + 1:], code.co_qualname, ln, "generator"))
            else:
                bad["other"] += 1
                if len(examples) < 12:
                    examples.append((code.co_filename[len(src) + 1:], code.co_qualname, ln, entry))
print(f"{res.testsRun} tests, {len(instrs)} code objects, {checked} executed instructions checked")
print("instructions that ran on a line with no LINE event in their code object:", bad)
for e in examples:
    print("  ", e)
sys.exit(1 if bad["other"] or bad["generator"] else 0)
