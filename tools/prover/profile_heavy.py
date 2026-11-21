import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
BUILD = Path(os.environ.get("PROVER_BUILD", TOOLS.parent.parent / "build"))
RUNROOT = BUILD / "runroot"
OUT = BUILD / "profiles"
PYTHON = Path(sys.executable)


def run(test):
    out = OUT / f"{test['number']}.pkl"
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=str(TOOLS), VALUE_PROFILE_OUT=str(out))
    env["PATH"] = f"{PYTHON.parent}:{env['PATH']}"
    start = time.time()
    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", "-p", "value_recorder", "-p", "no:cacheprovider", test["id"]],
        cwd=RUNROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    tail = result.stdout.strip().splitlines()[-1:] if result.stdout else []
    return test, result.returncode, time.time() - start, tail


def main(tests_json, threshold, jobs):
    tests = json.loads(Path(tests_json).read_text())["tests"]
    heavy = sorted((t for t in tests if t["seconds"] >= float(threshold)), key=lambda t: -t["seconds"])
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(int(jobs)) as pool:
        for test, code, elapsed, tail in pool.map(run, heavy):
            print(f"{test['number']:5} exit={code} {elapsed:7.1f}s (orig {test['seconds']:6.2f}s) {test['id']} {tail}", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
