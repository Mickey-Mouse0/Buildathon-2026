"""Record tokenizer calls and outside-the-tokenizer activity, into blast/tokreplay.pkl.xz.

    python tools/build_tokreplay.py <sqlglot dir> <tests.json> <out.pkl.xz> [shards] [seeds]

Runs sqlglot's suite with tools/tokreplay_plugin.py once per hash seed and merges:
code ranges that ran outside the tokenizer and settings read outside it (union over
seeds), each test's tokenizer calls (union). A test missing from a seed, failing,
starting a process or not replayable has no calls entry, so the selector keeps it.
The pinned commit's digests of every call are made at image build time
(blast/tokreplay.py base), in the image itself.
"""
import glob
import json
import lzma
import os
import pickle
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def merge_ranges(rs):
    out = {}
    for path, a, b in rs:
        out.setdefault(path, []).append((a, b))
    for path, v in out.items():
        v.sort()
        merged = []
        for a, b in v:
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        out[path] = merged
    return out


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
    raws = {seed: tempfile.mkdtemp(prefix=f"tokreplay-{seed}-") for seed in seeds.split(",")}

    def run(job):
        seed, files = job
        env = dict(os.environ, TOKREPLAY_OUT=raws[seed], SKIP_INTEGRATION="0", PYTHONPATH=str(TOOLS), PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tokreplay_plugin", "-p", "no:cacheprovider", *files],
                           cwd=src, env=env, capture_output=True, text=True)
        return seed, files, r.returncode, r.stdout[-300:]

    with ThreadPoolExecutor(n) as pool:
        for seed, files, code, tail in pool.map(run, [(s, g) for s in raws for g in groups if g]):
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            print(f"seed {seed}: shard of {len(files)} files exit={code} {last}", flush=True)

    outside, collect, wrapped, collect_calls, collect_bad = set(), set(), None, set(), []
    for raw in raws.values():
        for p in glob.glob(os.path.join(raw, "parent-*.pkl")):
            d = pickle.load(open(p, "rb"))
            outside |= d["outside"]
            collect |= d["collect"]
            wrapped = d["wrapped"] if wrapped is None else wrapped & d["wrapped"]
            collect_calls |= d["collect_calls"]
            if d["collect_bad"]:
                collect_bad.append(d["collect_bad"])
    seen, calls, reads, dropped = {}, {}, {}, {}
    for raw in raws.values():
        for p in glob.glob(os.path.join(raw, "t-*.pkl")):
            d = pickle.load(open(p, "rb"))
            t = d["test"]
            seen[t] = seen.get(t, 0) + 1
            outside |= d["outside"]
            reads.setdefault(t, set()).update(d["reads"])
            why = d["bad"] or ("failed" if d["failed"] else None) or ("started a process" if d["spawned"] else None)
            if why:
                dropped[t] = why
            calls.setdefault(t, set()).update(d["calls"])
    recorded = sorted(t for t, c in seen.items() if c == len(raws))
    inputs, index = [], {}

    def ref(c):
        i = index.get(c)
        if i is None:
            i = index[c] = len(inputs)
            inputs.append(c)
        return i

    test_calls = [None if t in dropped else sorted(ref(c) for c in calls[t]) for t in recorded]
    collect_idx = sorted(ref(c) for c in collect_calls)
    out_reads = {}
    for ti, t in enumerate(recorded):
        for a in reads[t]:
            out_reads.setdefault(a, []).append(ti)
    data = {"tests": recorded, "calls": test_calls, "inputs": inputs, "outside": merge_ranges(outside),
            "out_reads": out_reads, "collect": collect, "wrapped": wrapped or set(), "collect_calls": collect_idx,
            "collect_bad": collect_bad}
    with lzma.open(out, "wb", preset=9) as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    reasons = {}
    for why in dropped.values():
        reasons[why[:50]] = reasons.get(why[:50], 0) + 1
    print(f"tokreplay: {len(recorded)} tests recorded under {len(raws)} seeds, {len(dropped)} without calls "
          f"{sorted(reasons.items(), key=lambda kv: -kv[1])[:4]}, {len(inputs)} distinct calls "
          f"({sum(len(c[3]) for c in inputs)} chars), {len(collect_idx)} at collection, {len(wrapped or ())} settings "
          f"watched, {os.path.getsize(out) / 1e6:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:6])
