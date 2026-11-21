"""Check key-level reads on planted edits to class-level dict settings.

    python tools/keymap_bench.py <sqlglot dir> <tests.json> <work dir> <count> <seed> [jobs]

Plants one-line edits in dict settings (change a value, rename a key, delete an
entry), runs the selector on them with and without blast/keymap.pkl.gz, then runs
every test the key map dropped on the patched code. A dropped test that fails is a
VIOLATION: it would have been a 300 s miss.
"""
import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def candidates(src):
    out = []
    for path in sorted(Path(src, "sqlglot").rglob("*.py")):
        text = path.read_text()
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        starts = {}
        for n in ast.walk(tree):
            if hasattr(n, "lineno"):
                starts.setdefault(n.lineno, []).append(n)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for st in cls.body:
                value = st.value if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
                if not isinstance(value, ast.Dict) or value.lineno == value.end_lineno:
                    continue
                for k, v in zip(value.keys, value.values):
                    if k is None or k.lineno != v.end_lineno or k.lineno == value.lineno:
                        continue
                    line = lines[k.lineno - 1]
                    if sum(1 for kk in value.keys if kk is not None and kk.lineno == k.lineno) != 1:
                        continue
                    if any(isinstance(x, ast.Lambda) for x in ast.walk(v)):
                        continue
                    out.append((str(path.relative_to(src)), k.lineno, line, k, v))
    return out


def mutate(rng, line, k, v):
    ops = []
    if isinstance(k, ast.Constant) and isinstance(k.value, str):
        q = line[k.col_offset]
        lit = line[k.col_offset:k.end_col_offset]
        ops.append(("rename key", line[:k.col_offset] + lit[:-1] + "_X" + q + line[k.end_col_offset:]))
    consts = [c for c in ast.walk(v) if isinstance(c, ast.Constant) and c.lineno == c.end_lineno == k.lineno]
    for c in consts:
        seg = line[c.col_offset:c.end_col_offset]
        if isinstance(c.value, bool):
            new = "False" if c.value else "True"
        elif isinstance(c.value, int):
            new = str(c.value + 1)
        elif isinstance(c.value, str) and seg[-1:] in "'\"" and not seg.startswith(("b", "r", "f")):
            new = seg[:-1] + "X" + seg[-1]
        else:
            continue
        ops.append(("change value", line[:c.col_offset] + new + line[c.end_col_offset:]))
    if isinstance(v, ast.Attribute) and v.lineno == v.end_lineno:
        seg = line[v.col_offset:v.end_col_offset]
        if seg.startswith("TokenType."):
            ops.append(("change value", line[:v.col_offset] + "TokenType.VAR" + line[v.end_col_offset:]))
        if seg.startswith("exp."):
            ops.append(("change value", line[:v.col_offset] + "exp.Anonymous" + line[v.end_col_offset:]))
    ops.append(("delete entry", None))
    return rng.choice(ops)


def make_patch(src, rel, lineno, new_line):
    old = Path(src, rel).read_text().splitlines(keepends=True)
    new = list(old)
    if new_line is None:
        del new[lineno - 1]
    else:
        new[lineno - 1] = new_line + "\n"
    if new == old:
        return None
    body = "".join(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\n{body}"


def run_selector(data_dir, keymap):
    env = dict(os.environ, BLAST_DATA=str(data_dir), BLAST_SQLGLOT=os.environ["BENCH_SQLGLOT"],
               BLAST_KEYMAP=keymap)
    r = subprocess.run([sys.executable, str(REPO / "blast" / "selector.py")], capture_output=True, text=True, env=env)
    if r.returncode:
        sys.exit(r.stderr[-2000:])
    return {o["commit"]: {n for w in o["workers"] for n in w} for o in map(json.loads, r.stdout.splitlines())}, r.stderr


def run_tests(src, patch_text, ids, workdir):
    tree = Path(workdir)
    if tree.exists():
        shutil.rmtree(tree)
    shutil.copytree(src, tree, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    sys.path.insert(0, str(REPO / "blast"))
    import patchutil

    for fc in patchutil.parse(patch_text):
        p = tree / fc.path
        p.write_text(fc.apply(p.read_text()))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", *ids],
                       cwd=tree, capture_output=True, text=True, env=dict(os.environ, SKIP_INTEGRATION="0"))
    failed = sorted(set(re.findall(r"^(?:FAILED|ERROR|SUBFAILED)\S*\s+(\S+?)(?:\s|$)", r.stdout, re.M)))
    shutil.rmtree(tree)
    return failed, r.returncode


def main(src, tests_json, work, count, seed, jobs="6"):
    os.environ["BENCH_SQLGLOT"] = str(Path(src).resolve())
    rng = random.Random(int(seed))
    tests = json.loads(Path(tests_json).read_text())
    num2id = {t["number"]: t["id"] for t in tests["tests"]}
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    (work / "data" / "commits").mkdir(parents=True)
    shutil.copy(tests_json, work / "data" / "tests.json")
    cands = candidates(src)
    print(f"{len(cands)} dict entries to plant in", flush=True)
    rng.shuffle(cands)
    planted = {}
    for rel, lineno, line, k, v in cands:
        if len(planted) >= int(count):
            break
        kind, new_line = mutate(rng, line, k, v)
        text = make_patch(src, rel, lineno, new_line)
        if text is None:
            continue
        cid = f"m{len(planted):03d}"
        planted[cid] = (kind, rel, lineno)
        (work / "data" / "commits" / f"{cid}.patch").write_text(text)
    with_keys, log_keys = run_selector(work / "data", str(REPO / "blast" / "keymap.pkl.gz"))
    without, _ = run_selector(work / "data", "/nonexistent")
    (work / "selector.log").write_text(log_keys)

    def check(cid):
        dropped = sorted(without[cid] - with_keys[cid])
        if not dropped:
            return cid, dropped, [], 0
        text = (work / "data" / "commits" / f"{cid}.patch").read_text()
        failed, code = run_tests(src, text, [num2id[n] for n in dropped], work / f"tree-{cid}")
        return cid, dropped, failed, code

    results = {}
    violations = 0
    with ThreadPoolExecutor(int(jobs)) as pool:
        for cid, dropped, failed, code in pool.map(check, sorted(planted)):
            kind, rel, lineno = planted[cid]
            results[cid] = {"kind": kind, "where": f"{rel}:{lineno}", "attr_level": len(without[cid]),
                            "key_level": len(with_keys[cid]), "dropped": len(dropped), "failed": failed, "exit": code}
            bad = bool(failed) or code not in (0, 5)
            violations += bad
            print(f"{cid} {kind:13} {rel}:{lineno:<5} attr-level {len(without[cid]):4} key-level {len(with_keys[cid]):4} "
                  f"ran {len(dropped):4} dropped {'VIOLATION ' + str(failed[:3]) + f' exit={code}' if bad else 'ok'}", flush=True)
    (work / "results.json").write_text(json.dumps(results, indent=1))
    dropped_total = sum(r["dropped"] for r in results.values())
    print(f"{len(results)} planted edits, {dropped_total} dropped tests run on the patched code, {violations} violations")


if __name__ == "__main__":
    main(*sys.argv[1:7])
