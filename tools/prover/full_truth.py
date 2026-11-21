import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import materialize  # noqa: E402

OUTCOME = re.compile(r"^(FAILED|ERROR|SUBFAILED)\b")
NODEID = re.compile(r"^\S+?(?:\[.*\])?\s+(tests/[^\s\[\]]+?\.py::[^\s\[\]]+)")


def failing_tests(run_dir, jobs, timeout):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-rA", "-n", str(jobs), "-p", "no:cacheprovider",
             "-o", "faulthandler_timeout=0", "tests"],
            cwd=run_dir, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    failed = set()
    for line in proc.stdout.splitlines():
        if OUTCOME.match(line):
            ids = NODEID.findall(line)
            if ids:
                failed.add(ids[0])
    tail = proc.stdout.strip().splitlines()[-1:] if proc.stdout else []
    if proc.returncode not in (0, 1):
        return None, f"pytest exit {proc.returncode} {tail}"
    return failed, tail


def main(bench_dir, tests_json, sqlglot_dir, jobs="8", timeout="900", only="", answers_name="answers.json"):
    bench = Path(bench_dir).resolve()
    sqlglot_dir = Path(sqlglot_dir).resolve()
    tests = json.loads(Path(tests_json).read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    meta = json.loads((bench / "meta.json").read_text())
    answers_path = bench / answers_name
    answers = json.loads(answers_path.read_text())["commits"] if answers_path.exists() else {}
    wanted = set(only.split(",")) if only else None
    for cid, info in sorted(meta.items()):
        if cid in answers or (wanted is not None and cid not in wanted):
            continue
        run_dir = bench / "run" / cid
        materialize((bench / "commits" / f"{cid}.patch").read_text(), run_dir, sqlglot_dir)
        failed, tail = failing_tests(run_dir, jobs, int(timeout))
        shutil.rmtree(run_dir, ignore_errors=True)
        if failed is None:
            failing = sorted(number.values())
            note = f"unusable run ({tail}); every test counted as failing"
        else:
            unknown = sorted(f for f in failed if f not in number)
            failing = sorted(number[f] for f in failed if f in number)
            note = f"{tail} unknown={unknown[:3]}" if unknown else str(tail)
        answers[cid] = {"failing": failing, "kind": info["kind"]}
        answers_path.write_text(json.dumps({"commits": answers}))
        print(f"{cid} {info['kind']:8} {info['files']} failing={len(failing)} {note}", flush=True)
    shutil.copy(tests_json, bench / "tests.json")


if __name__ == "__main__":
    main(*sys.argv[1:8])
