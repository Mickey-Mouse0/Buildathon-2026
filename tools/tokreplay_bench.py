"""Check tokenizer replay on planted edits to tokenizer code and settings.

    python tools/tokreplay_bench.py <sqlglot dir> <tests.json> <work dir> <count> <seed> [jobs]

Plants one-line mutations (operators, comparisons, constants, and/or, not) in
sqlglot/tokenizer_core.py, sqlglot/tokens.py and the Tokenizer classes of dialects,
runs the selector with and without blast/tokreplay.pkl.xz, then runs every test that
replay dropped on the patched code. A dropped test that fails is a VIOLATION.
"""
import ast
import json
import random
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from keymap_bench import REPO, make_patch, run_selector, run_tests  # noqa: E402

OPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r" and ", " or "), (r" or ", " and "), (r"\bnot ", ""), (r" \+ 1\b", " + 2"), (r" - 1\b", " - 2"),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"(?<![\w.])(\d+)(?![\w.])", None), (r" is None", " is not None"),
    (r" is not None", " is None"), (r" in ", " not in "),
]


def lines_to_plant(src):
    out = []
    files = [Path(src, "sqlglot", "tokenizer_core.py"), Path(src, "sqlglot", "tokens.py")]
    files += sorted(Path(src, "sqlglot", "dialects").glob("*.py"))
    for path in files:
        text = path.read_text()
        tree = ast.parse(text)
        lines = text.splitlines()
        spans = []
        if path.parent.name == "dialects":
            spans = [(n.lineno + 1, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Tokenizer"]
        else:
            spans = [(n.body[0].lineno, n.end_lineno) for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.body]
        for a, b in spans:
            for l in range(a, b + 1):
                s = lines[l - 1]
                if s.strip() and not s.strip().startswith(("#", '"""', "def ", "class ", "@")):
                    out.append((str(path.relative_to(src)), l, s))
    return sorted(set(out))


def mutate(rng, line):
    ops = []
    for pat, rep in OPS:
        for m in re.finditer(pat, line):
            if rep is None:
                new = line[:m.start()] + str(int(m.group(1)) + 1) + line[m.end():]
            else:
                new = line[:m.start()] + rep + line[m.end():]
            ops.append(new)
    rng.shuffle(ops)
    for new in ops:
        try:
            ast.parse(new.strip() if not new.strip().endswith(":") else new.strip() + " pass")
        except SyntaxError:
            continue
        return new
    return None


def main(src, tests_json, work, count, seed, jobs="6"):
    import os

    os.environ["BENCH_SQLGLOT"] = str(Path(src).resolve())
    rng = random.Random(int(seed))
    tests = json.loads(Path(tests_json).read_text())
    num2id = {t["number"]: t["id"] for t in tests["tests"]}
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    (work / "data" / "commits").mkdir(parents=True)
    shutil.copy(tests_json, work / "data" / "tests.json")
    cands = lines_to_plant(src)
    rng.shuffle(cands)
    planted = {}
    for rel, lineno, line in cands:
        if len(planted) >= int(count):
            break
        new = mutate(rng, line)
        if new is None:
            continue
        text = make_patch(src, rel, lineno, new)
        try:
            full = Path(src, rel).read_text().splitlines(keepends=True)
            full[lineno - 1] = new + "\n"
            ast.parse("".join(full))
        except SyntaxError:
            continue
        cid = f"t{len(planted):03d}"
        planted[cid] = (rel, lineno, line.strip(), new.strip())
        (work / "data" / "commits" / f"{cid}.patch").write_text(text)
    os.environ.pop("BLAST_TOKREPLAY", None)
    with_replay, log = run_selector(work / "data", str(REPO / "blast" / "keymap.pkl.gz"))
    os.environ["BLAST_TOKREPLAY"] = "/nonexistent"
    without, _ = run_selector(work / "data", str(REPO / "blast" / "keymap.pkl.gz"))
    os.environ.pop("BLAST_TOKREPLAY")
    (work / "selector.log").write_text(log)

    def check(cid):
        dropped = sorted(without[cid] - with_replay[cid])
        if not dropped:
            return cid, dropped, [], 0
        text = (work / "data" / "commits" / f"{cid}.patch").read_text()
        failed, code = run_tests(src, text, [num2id[n] for n in dropped], work / f"tree-{cid}")
        return cid, dropped, failed, code

    violations, results = 0, {}
    with ThreadPoolExecutor(int(jobs)) as pool:
        for cid, dropped, failed, code in pool.map(check, sorted(planted)):
            rel, lineno, old, new = planted[cid]
            bad = bool(failed) or code not in (0, 5)
            violations += bad
            results[cid] = {"where": f"{rel}:{lineno}", "old": old, "new": new, "without": len(without[cid]),
                            "with": len(with_replay[cid]), "dropped": len(dropped), "failed": failed}
            print(f"{cid} {rel}:{lineno:<5} without {len(without[cid]):4} with {len(with_replay[cid]):4} ran {len(dropped):4} "
                  f"dropped {'VIOLATION ' + str(failed[:3]) + f' exit={code}' if bad else 'ok'}  | {new.strip()[:60]}",
                  flush=True)
    (work / "results.json").write_text(json.dumps(results, indent=1))
    print(f"{len(results)} planted edits, {sum(r['dropped'] for r in results.values())} dropped tests run, "
          f"{violations} violations")


if __name__ == "__main__":
    main(*sys.argv[1:7])
