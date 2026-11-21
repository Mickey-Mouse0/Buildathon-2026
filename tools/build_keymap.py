"""Record which keys of each class-level dict every test reads, into blast/keymap.pkl.gz.

    python tools/build_keymap.py <sqlglot dir> <tests.json> <out.pkl.gz> [shards] [seeds]

Runs sqlglot's suite with tools/keymap_plugin.py (one fork per test), split by file
across shards, once per hash seed (default 0,1,2: code that walks a set can look up
different keys under another seed), then takes the union of what each test read. A
test missing from any seed's run, or that started another process, is left out, so
the selector keeps its attribute-level reads. Use the same Python and packages as
the scorer's image (tools/build_keymap_docker.sh does).
"""
import glob
import gzip
import json
import os
import pickle
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def main(src, tests_json, out, shards="8", seeds="0,1,2"):
    tests = json.loads(Path(tests_json).read_text())["tests"]
    by_file = {}
    for t in tests:
        by_file[t["file"]] = by_file.get(t["file"], 0.0) + t["seconds"]
    n = int(shards)
    groups, loads = [[] for _ in range(n)], [0.0] * n
    for path, seconds in sorted(by_file.items(), key=lambda kv: -kv[1]):
        i = loads.index(min(loads))
        groups[i].append(path)
        loads[i] += seconds
    raws = {seed: tempfile.mkdtemp(prefix=f"keymap-{seed}-") for seed in seeds.split(",")}

    def run(job):
        seed, files = job
        env = dict(os.environ, KEYMAP_OUT=raws[seed], SKIP_INTEGRATION="0", PYTHONPATH=str(TOOLS), PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "keymap_plugin", "-p", "no:cacheprovider", *files],
                           cwd=src, env=env, capture_output=True, text=True)
        return seed, files, r.returncode, r.stdout[-300:]

    jobs = [(seed, g) for seed in raws for g in groups if g]
    with ThreadPoolExecutor(n) as pool:
        for seed, files, code, tail in pool.map(run, jobs):
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            print(f"seed {seed}: shard of {len(files)} files exit={code} {last}", flush=True)

    metas = [pickle.load(open(p, "rb")) for raw in raws.values() for p in glob.glob(os.path.join(raw, "meta-*.pkl"))]
    first = metas[0]
    for m in metas[1:]:
        if m["groups"] != first["groups"] or m["unsafe"] != first["unsafe"]:
            sys.exit("keymap: shards wrapped different dicts")
    groups_list = [tuple(first["groups"][g]) for g in sorted(first["groups"])]
    gindex = {g: i for i, g in enumerate(groups_list)}
    per_test, seen, spawned, failed = {}, {}, set(), set()
    for raw in raws.values():
        for p in glob.glob(os.path.join(raw, "t-*.pkl")):
            d = pickle.load(open(p, "rb"))
            t = d["test"]
            seen[t] = seen.get(t, 0) + 1
            if d["failed"]:
                failed.add(t)
            if d.get("spawned"):
                spawned.add(t)
            mine = per_test.setdefault(t, {})
            for g, ks in d["reads"].items():
                mine.setdefault(g, set()).update(ks)
    # a test that failed under the recorder may have stopped early and read less
    recorded = sorted(t for t, c in seen.items() if c == len(raws) and t not in spawned and t not in failed)
    reads = [dict() for _ in groups_list]
    for ti, t in enumerate(recorded):
        for g, ks in per_test[t].items():
            r = reads[gindex[g]]
            for k in ks:
                r.setdefault(k, []).append(ti)
    collect = [set() for _ in groups_list]
    for raw in raws.values():
        for p in glob.glob(os.path.join(raw, "collect-*.pkl")):
            for g, ks in pickle.load(open(p, "rb"))["reads"].items():
                collect[gindex[g]] |= ks
    failed = sorted(failed)
    data = {"tests": recorded, "failed": failed, "groups": groups_list, "reads": reads,
            "collect": collect, "unsafe": set(first["unsafe"])}
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    expected = {t["id"] for t in tests}
    missing = expected - set(recorded)
    print(f"keymap: {len(recorded)} tests recorded under {len(raws)} seeds, {len(spawned)} started a process "
          f"({sorted(spawned)[:3]}), {len(missing)} of tests.json missing, {len(failed)} failed under the "
          f"recorder, {len(groups_list)} dicts, {len(first['unsafe'])} left alone, {os.path.getsize(out) / 1e6:.2f} MB",
          file=sys.stderr)
    if missing:
        print("missing:", sorted(missing)[:10], file=sys.stderr)
    if failed:
        print("failed:", failed[:10], file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:5])
