import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "blast" / "prover"))

from prove import Prover, apply_hunks, load_packaged, load_profile_dir, split_patch  # noqa: E402

PYTHON = Path(os.environ.get("BENCH_PYTHON", sys.executable))
RESULT = re.compile(r"^(PASSED|FAILED|ERROR|SUBFAILED|SUBPASSED)\b")
NODEID = re.compile(r"^\S+?(?:\[.*\])?\s+(tests/[^\s\[\]]+?\.py::[^\s\[\]]+)")


def materialize(patch_text, run_dir, sqlglot_dir):
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    shutil.copytree(sqlglot_dir / "sqlglot", run_dir / "sqlglot")
    for entry in sqlglot_dir.iterdir():
        if entry.name not in ("sqlglot", "tests", ".git"):
            (run_dir / entry.name).symlink_to(entry)
    entries = split_patch(patch_text)
    if any(e["new"].removeprefix("b/").startswith("tests/") for e in entries):
        shutil.copytree(sqlglot_dir / "tests", run_dir / "tests")
    else:
        (run_dir / "tests").symlink_to(sqlglot_dir / "tests")
    for entry in entries:
        path = entry["new"].removeprefix("b/")
        target = run_dir / path
        target.write_text(apply_hunks(target.read_text(), entry["hunks"]))


def run_tests(run_dir, test_ids, timeout):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
    env["PATH"] = f"{PYTHON.parent}:{env['PATH']}"
    try:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q", *test_ids],
            cwd=run_dir, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {t: "timeout" for t in test_ids}
    status = {t: "missing" for t in test_ids}
    for line in proc.stdout.splitlines():
        m = RESULT.match(line)
        ids = NODEID.findall(line) if m else []
        if not ids:
            continue
        outcome, nodeid = m.group(1), ids[0]
        if nodeid in status:
            if outcome in ("FAILED", "ERROR", "SUBFAILED"):
                status[nodeid] = "failed"
            elif status[nodeid] == "missing" and outcome == "PASSED":
                status[nodeid] = "passed"
    if proc.returncode not in (0, 1):
        for t, s in status.items():
            if s == "missing":
                status[t] = f"crash({proc.returncode})"
    return status


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bench", required=True)
    p.add_argument("--tests", required=True)
    p.add_argument("--sqlglot", required=True)
    p.add_argument("--coverage")
    p.add_argument("--profiles")
    p.add_argument("--index")
    p.add_argument("--packaged", help="prover_data directory; replaces --coverage/--profiles/--index")
    p.add_argument("--jobs", type=int, default=6)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--check-all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--union", action="store_true", help="check whole-commit claims of the all-tests replay")
    args = p.parse_args()

    tests = {t["number"]: t for t in json.loads(Path(args.tests).read_text())["tests"]}
    if args.packaged:
        prover = load_packaged(args.packaged, args.sqlglot, list(tests.values()))
        coverage = json.loads(Path(args.coverage).read_text()) if args.coverage else {"lines": prover.lines}
    else:
        coverage = json.loads(Path(args.coverage).read_text())
        prover = Prover(args.sqlglot, tests, coverage, load_profile_dir(args.profiles), json.loads(Path(args.index).read_text()))
    bench = Path(args.bench).resolve()
    meta = json.loads((bench / "meta.json").read_text())
    sqlglot_dir = Path(args.sqlglot).resolve()

    jobs = []
    for mid, info in sorted(meta.items()):
        patch = (bench / "commits" / f"{mid}.patch").read_text()
        verdict = prover.verdict(patch)
        if args.union:
            proven, why = prover.union_verdict(patch)
            verdict = {"noop": False, "safe": {}, "notes": [why]}
            if proven:
                verdict = {"noop": True, "safe": {t: why for t in prover.union_profiled}, "notes": [why]}
        touching = set()
        for changed in info.get("files", [info["file"]]):
            touching |= {t for ts in coverage["lines"].get(changed, {}).values() for t in ts}
        if args.union:
            to_check = sorted(t for t in verdict["safe"] if t in touching) if verdict["noop"] else []
        else:
            to_check = sorted(t for t in verdict["safe"] if t in touching or verdict["noop"] or args.check_all)
        jobs.append((mid, info, patch, verdict, to_check))

    def work(job):
        mid, info, patch, verdict, to_check = job
        if not to_check or args.dry_run:
            return mid, info, verdict, {}
        run_dir = bench / "run" / mid
        materialize(patch, run_dir, sqlglot_dir)
        status = run_tests(run_dir, [tests[t]["id"] for t in to_check], args.timeout)
        shutil.rmtree(run_dir, ignore_errors=True)
        return mid, info, verdict, {t: status[tests[t]["id"]] for t in to_check}

    results = {}
    with ThreadPoolExecutor(args.jobs) as pool:
        for mid, info, verdict, checked in pool.map(work, jobs):
            bad = {t: s for t, s in checked.items() if s != "passed"}
            results[mid] = {"info": info, "safe": {str(k): v for k, v in verdict["safe"].items()}, "noop": verdict["noop"],
                            "notes": verdict["notes"], "checked": {str(k): v for k, v in checked.items()}}
            flag = "VIOLATION" if bad else "ok"
            print(f"{mid} {info['kind']:11} {info['file']}:{info['line']} safe={len(verdict['safe'])} "
                  f"checked={len(checked)} {flag} {bad if bad else ''}", flush=True)
    (bench / "results.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
