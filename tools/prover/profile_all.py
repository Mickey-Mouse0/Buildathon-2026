import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PYTHON = Path(sys.executable)


def main(tests_json, runroot, out_dir, seed, shards="8", skip_over_seconds="0.3"):
    tests = json.loads(Path(tests_json).read_text())["tests"]
    skip = [t["id"] for t in tests if t["seconds"] >= float(skip_over_seconds)]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    skip_file = out / "skip.txt"
    skip_file.write_text("\n".join(skip))
    by_file = {}
    for t in tests:
        if t["id"] not in skip:
            by_file.setdefault(t["file"], 0.0)
            by_file[t["file"]] += t["seconds"]
    groups = [[] for _ in range(int(shards))]
    loads = [0.0] * int(shards)
    for path, seconds in sorted(by_file.items(), key=lambda kv: -kv[1]):
        i = loads.index(min(loads))
        groups[i].append(path)
        loads[i] += seconds

    def run(files):
        env = dict(os.environ, PYTHONHASHSEED=str(seed), SKIP_INTEGRATION="0", PYTHONPATH=str(TOOLS),
                   VALUE_PROFILE_FORK_DIR=str(out), VALUE_PROFILE_SKIP=str(skip_file))
        env["PATH"] = f"{PYTHON.parent}:{env['PATH']}"
        result = subprocess.run([str(PYTHON), "-m", "pytest", "-q", "-p", "value_recorder", "-p", "no:cacheprovider", *files],
                                cwd=runroot, env=env, capture_output=True, text=True)
        return files, result.returncode

    with ThreadPoolExecutor(int(shards)) as pool:
        for files, code in pool.map(run, [g for g in groups if g]):
            print(f"shard of {len(files)} files exit={code}", flush=True)
    done = len(list(out.glob("*.pkl")))
    print(f"{done} test profiles written ({len(tests) - len(skip)} expected)", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
