"""Find misses a new selector version adds over an old one.

    python tools/diff_bench.py <sqlglot dir> <old repo> <new repo> <tests.json> <work dir> <count> <seed> [jobs]

Generates a practice-like set (tools/prover/make_eval_set.py), runs both selectors on
it, and runs every test the new one leaves out but the old one kept on the patched
code. A failure there is a miss the new version introduced. Run it inside the
scorer's image (see CLAUDE.md).
"""
import gzip
import json
import lzma
import os
import pickle
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from keymap_bench import run_tests  # noqa: E402


def prepare(repo, src):
    repo = Path(repo)
    pd = repo / "blast" / "prover_data"
    for name in ("profiles", "union"):
        if (pd / f"{name}.pkl.xz").exists() and not (pd / f"{name}.pkl").exists():
            with lzma.open(pd / f"{name}.pkl.xz") as f, open(pd / f"{name}.pkl", "wb") as g:
                shutil.copyfileobj(f, g)
    if (repo / "tools" / "build_importstate.sh").exists():
        subprocess.run(["sh", str(repo / "tools" / "build_importstate.sh"), src, str(repo / "blast" / "importstate.pkl")],
                       check=True, capture_output=True)
    if (repo / "blast" / "tokreplay.py").exists():
        subprocess.run([sys.executable, str(repo / "blast" / "tokreplay.py"), "base", src,
                        str(repo / "blast" / "tokreplay.pkl.xz"), str(repo / "blast" / "tokreplay_base.pkl")],
                       check=True, capture_output=True, env=dict(os.environ, PYTHONHASHSEED="0"))


def select(repo, data, src):
    env = dict(os.environ, BLAST_DATA=str(data), BLAST_SQLGLOT=src)
    r = subprocess.run([sys.executable, str(Path(repo) / "blast" / "selector.py")], capture_output=True, text=True, env=env)
    if r.returncode:
        sys.exit(f"selector in {repo} failed: {r.stderr[-1500:]}")
    return {o["commit"]: {n for w in o["workers"] for n in w} for o in map(json.loads, r.stdout.splitlines())}, r.stderr


def main(src, old, new, tests_json, work, count, seed, jobs="4"):
    src = str(Path(src).resolve())
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for repo in (old, new):
        prepare(repo, src)
    with gzip.open(Path(new) / "blast" / "covmap.pkl.gz") as f:
        m = pickle.load(f)
    (work / "map.json").write_text(json.dumps({"lines": {p: sorted(ls) for p, ls in m["line2t"].items()}}))
    data = work / "ev"
    subprocess.run([sys.executable, str(Path(new) / "tools" / "prover" / "make_eval_set.py"), src, str(work / "map.json"),
                    str(data), count, seed], check=True)
    shutil.copy(tests_json, data / "tests.json")
    sel_old, _ = select(old, data, src)
    sel_new, log_new = select(new, data, src)
    (work / "new.log").write_text(log_new)
    num2id = {t["number"]: t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}
    meta = json.loads((data / "meta.json").read_text())

    def check(cid):
        dropped = sorted(sel_old[cid] - sel_new[cid])
        if not dropped:
            return cid, dropped, [], 0
        text = (data / "commits" / f"{cid}.patch").read_text()
        failed, code = run_tests(src, text, [num2id[n] for n in dropped], work / f"tree-{cid}")
        return cid, dropped, failed, code

    bad = 0
    with ThreadPoolExecutor(int(jobs)) as pool:
        for cid, dropped, failed, code in pool.map(check, sorted(sel_old)):
            miss = bool(failed) or code not in (0, 5)
            bad += miss
            if dropped or miss:
                print(f"{cid} {meta[cid]['kind']:8} {'+'.join(meta[cid]['edits']):18} {meta[cid]['file']:45} "
                      f"old {len(sel_old[cid]):4} new {len(sel_new[cid]):4} ran {len(dropped):4} "
                      f"{'MISS ' + str(failed[:3]) + f' exit={code}' if miss else 'ok'}", flush=True)
    print(f"{len(sel_old)} commits, {bad} with a failing test the new version drops")


if __name__ == "__main__":
    main(*sys.argv[1:9])
