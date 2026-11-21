import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import materialize  # noqa: E402

FAILED = re.compile(r"^(FAILED|ERROR|SUBFAILED)\b")
NODEID = re.compile(r"^\S+?(?:\[.*\])?\s+(tests/[^\s\[\]]+?\.py::[^\s\[\]]+)")


def check(bench, commit, ids, sqlglot_dir, jobs):
    run_dir = bench / "run-dropped" / commit
    materialize((bench / "commits" / f"{commit}.patch").read_text(), run_dir, sqlglot_dir)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-n", str(jobs), "-p", "no:cacheprovider", *ids],
                              cwd=run_dir, env=env, capture_output=True, text=True, timeout=1200)
        failed = {NODEID.match(line).group(1) for line in proc.stdout.splitlines()
                  if FAILED.match(line) and NODEID.match(line)}
        status = proc.returncode
    except subprocess.TimeoutExpired:
        failed, status = {"TIMEOUT"}, "timeout"
    shutil.rmtree(run_dir, ignore_errors=True)
    return commit, status, sorted(failed)


def main(bench_dir, dropped_json, tests_json, sqlglot_dir, selector_log, parallel="3", jobs="4"):
    bench = Path(bench_dir).resolve()
    tests = {t["number"]: t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}
    dropped = json.loads(Path(dropped_json).read_text())
    notes = {}
    for line in Path(selector_log).read_text().splitlines():
        cid, _, rest = line.partition(" ")
        notes.setdefault(cid, []).append(rest[:220])
    work = [(c, [tests[n] for n in ns]) for c, ns in sorted(dropped.items())]
    bad = 0
    with ThreadPoolExecutor(int(parallel)) as pool:
        for commit, status, failed in pool.map(lambda w: check(bench, w[0], w[1], Path(sqlglot_dir).resolve(), jobs), work):
            if failed:
                bad += 1
                print(f"MISS {commit}: dropped tests that fail: {failed[:6]}", flush=True)
                for n in notes.get(commit, [])[:6]:
                    print(f"    {n}", flush=True)
            else:
                print(f"ok   {commit} ({len(dropped[commit])} dropped tests pass, pytest exit {status})", flush=True)
    print(f"\n{bad} commits where a dropped test fails", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:8])
