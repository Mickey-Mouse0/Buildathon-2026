import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coverage import CoverageData

TOOLS = Path(__file__).resolve().parent


def run(test, sqlglot_root, work):
    tag = str(test["number"])
    cov_file = work / f"{tag}.coverage"
    reads_dir = work / f"{tag}.reads"
    env = dict(
        os.environ,
        SKIP_INTEGRATION="0",
        PYTHONPATH=f"{sqlglot_root}:{TOOLS}",
        FIXTURE_TRACKER_OUT=str(reads_dir),
        FIXTURE_TRACKER_ALL_PHASES="1",
        COVERAGE_FILE=str(cov_file),
    )
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "fixture_tracker", "-p", "no:cacheprovider",
         f"--cov={sqlglot_root}/sqlglot", "--cov-report=", test["id"]],
        cwd=sqlglot_root, env=env, capture_output=True, text=True,
    )
    lines = {}
    data = CoverageData(str(cov_file))
    data.read()
    root = str(Path(sqlglot_root).resolve()) + "/"
    for measured in data.measured_files():
        rel = str(Path(measured).resolve()).removeprefix(root)
        lines[rel] = sorted(data.lines(measured) or [])
    reads = set()
    for path in reads_dir.glob("*.jsonl"):
        for raw in path.read_text().splitlines():
            reads.update(json.loads(raw)[1])
    for path in reads_dir.glob("*.outside"):
        reads.update(path.read_text().split("\n"))
    reads.discard("")
    return test, result.returncode, lines, sorted(reads)


def main(heavy_map_gz, tests_json, sqlglot_root, work_dir, out_gz, jobs="6"):
    import gzip

    with gzip.open(heavy_map_gz, "rt") as f:
        heavy = json.load(f)
    tests = {t["id"]: t for t in json.loads(Path(tests_json).read_text())["tests"]}
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    targets = [tests[i] for i in heavy["tests"]]
    position = {test_id: k for k, test_id in enumerate(heavy["tests"])}
    added_lines = added_reads = 0
    with ThreadPoolExecutor(int(jobs)) as pool:
        for test, code, lines, reads in pool.map(lambda t: run(t, sqlglot_root, work), targets):
            k = position[test["id"]]
            for path, numbers in lines.items():
                per_file = heavy["lines"].setdefault(path, {})
                for n in numbers:
                    holders = per_file.setdefault(str(n), [])
                    if k not in holders:
                        holders.append(k)
                        added_lines += 1
            for read in reads:
                holders = heavy["fixtures"].setdefault("tests/" + read, [])
                if k not in holders:
                    holders.append(k)
                    added_reads += 1
            print(f"{test['number']:5} exit={code} files={len(lines)} reads={len(reads)} {test['id']}", flush=True)
    with gzip.open(out_gz, "wt", compresslevel=9) as f:
        json.dump(heavy, f, separators=(",", ":"))
    print(f"added {added_lines} line attributions and {added_reads} file reads", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
