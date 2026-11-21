"""Planted fixture edits: blast/fixturecheck.py's verdict against the real tests.

Run inside the scorer's image with requirements.lock installed (pytest, duckdb):
    python tools/fixture_bench.py /opt/sqlglot N_PER_FILE SEED

For each planted edit it asks fixturecheck which tests it would drop, then runs those
tests on the patched tree. A dropped test that fails is a miss and the bench exits 1.
"""
import difflib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blast"))
sys.path.insert(0, str(ROOT / "tools" / "prover"))

PLUGIN = '''
import os
FAILED = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid.split("[")[0].strip())
def pytest_collectreport(report):
    if report.failed:
        FAILED.add("COLLECT:" + report.nodeid)
def pytest_sessionfinish(session):
    with open(os.environ["FAILREC_OUT"], "w") as f:
        f.write("\\n".join(sorted(FAILED)))
'''

WORD = re.compile(r"\S+")


def mutate(line, rng):
    words = WORD.findall(line)
    kind = rng.randrange(10)
    if kind == 0 and "SELECT " in line:
        return line.replace("SELECT ", "SELECT DISTINCT ", 1)
    if kind == 1:
        for a, b in ((" > ", " <= "), (" = ", " <> "), (" AND ", " OR "), (" + ", " - "), (" < ", " >= ")):
            if a in line:
                return line.replace(a, b, 1)
    if kind == 2 and len(words) > 1:
        i = rng.randrange(len(words))
        return " ".join(words[:i] + words[i + 1:])
    if kind == 3 and words:
        i = rng.randrange(len(words))
        return " ".join(words[:i + 1] + words[i:])
    if kind == 4 and len(words) > 1:
        i = rng.randrange(len(words) - 1)
        words[i], words[i + 1] = words[i + 1], words[i]
        return " ".join(words)
    if kind == 5:
        return line.lower()
    if kind == 6 and words:
        return line.rstrip(";") + " AS zz" + (";" if line.endswith(";") else "")
    if kind == 7:
        return re.sub(r"\d+", lambda m: str(int(m.group()) + 7), line, count=1)
    if kind == 8:
        return line.replace("(", "( ", 1)
    if kind == 9 and words:
        i = rng.randrange(len(words))
        return " ".join(words[:i] + ["x"] + words[i:])
    return line + " "


def main(src, per_file, seed):
    sys.path.insert(1, src)
    import patchutil
    from bench import materialize

    import sqlglot  # noqa: F401  the selector has the pinned sqlglot imported by now
    import fixturecheck

    src = Path(src)
    rng = random.Random(seed)
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    idof = {n: i for i, n in by_id.items()}
    plugdir = Path(tempfile.mkdtemp())
    (plugdir / "benchrec.py").write_text(PLUGIN)
    stats = {"dropped_pass": 0, "kept_fail": 0, "kept_pass": 0, "MISS": 0}
    for path, checks in fixturecheck.CHECKS.items():
        old = (src / path).read_text(encoding="utf-8")
        lines = old.split("\n")
        cand = [i for i, ln in enumerate(lines) if ln and not ln.startswith(("--", "#"))]
        chosen = [by_id[t] for t in checks if t in by_id]
        for k in range(per_file):
            new_lines = list(lines)
            for i in rng.sample(cand, rng.choice((1, 1, 1, 2))):
                new_lines[i] = mutate(new_lines[i], rng)
            new = "\n".join(new_lines)
            if new == old:
                continue
            patch = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                 f"a/{path}", f"b/{path}"))
            patch = f"diff --git a/{path} b/{path}\n" + patch
            notes = []
            safe = fixturecheck.judge(patchutil.parse(patch), chosen, by_id, str(src), notes)
            with tempfile.TemporaryDirectory() as td:
                run = Path(td) / "run"
                materialize(patch, run, src)
                rec = Path(td) / "failed.txt"
                env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONHASHSEED="0", FAILREC_OUT=str(rec),
                           PYTHONPATH=str(plugdir))
                subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "benchrec",
                                *[idof[n] for n in chosen]], cwd=run, env=env, capture_output=True, timeout=600)
                failed = set(rec.read_text().split()) if rec.exists() else {"?"}
            for n in chosen:
                fails = idof[n] in failed or any(f.startswith("COLLECT:") or f == "?" for f in failed)
                if n in safe:
                    key = "MISS" if fails else "dropped_pass"
                else:
                    key = "kept_fail" if fails else "kept_pass"
                stats[key] += 1
                if key == "MISS":
                    print("MISS", path, idof[n], patch, flush=True)
            print(path.rsplit("/", 1)[1], k, {idof[n].rsplit("::", 1)[1]: (n in safe) for n in chosen},
                  "failed:", sorted(f.rsplit("::", 1)[1] for f in failed), flush=True)
    print(json.dumps(stats))
    return 1 if stats["MISS"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
