"""Check the TPC executor check on mutations where it decides something.

    python tools/tpc_bench.py <sqlglot dir> <old repo> <new repo> <tests.json> <work dir> <count> <seed> [jobs]

Plants one mutation (tools/prover/mutants.py kinds) on a line that some but not all
queries of a TPC executor test reach (blast/tpc_map.pkl.gz), runs both selectors,
and runs every test the new one drops but the old one kept on the patched code.
"""
import gzip
import json
import pickle
import random
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "prover"))

from diff_bench import prepare, select  # noqa: E402
from keymap_bench import run_tests  # noqa: E402
from mutants import candidate_mutations, make_patch  # noqa: E402


def main(src, old, new, tests_json, work, count, seed, jobs="3"):
    src = str(Path(src).resolve())
    rng = random.Random(int(seed))
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    data = work / "ev"
    (data / "commits").mkdir(parents=True)
    shutil.copy(tests_json, data / "tests.json")
    for repo in (old, new):
        prepare(repo, src)
    with gzip.open(Path(new) / "blast" / "tpc_map.pkl.gz") as f:
        qmaps = pickle.load(f)
    partial = {}
    for qm in qmaps.values():
        n = len(qm["tests"])
        for path, lines in qm["line2t"].items():
            if path.startswith("sqlglot/"):
                for l, qs in lines.items():
                    if 0 < len(qs) < n:
                        partial.setdefault(path, set()).add(l)
    pool = []
    for path, lines in sorted(partial.items()):
        text = (Path(src) / path).read_text()
        for kind, line, new_text in candidate_mutations(path, text):
            if line in lines and kind != "comment":
                pool.append((path, text, new_text, kind, line))
    rng.shuffle(pool)
    meta = {}
    for i, (path, text, new_text, kind, line) in enumerate(pool[:int(count)]):
        cid = f"p{i:03d}"
        (data / "commits" / f"{cid}.patch").write_text(make_patch(path, text, new_text))
        meta[cid] = f"{kind} {path}:{line}"
    print(f"{len(pool)} candidate mutations on partly reached lines, planted {len(meta)}", flush=True)
    sel_old, _ = select(old, data, src)
    sel_new, log_new = select(new, data, src)
    (work / "new.log").write_text(log_new)
    num2id = {t["number"]: t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}

    def check(cid):
        dropped = sorted(sel_old[cid] - sel_new[cid])
        if not dropped:
            return cid, dropped, [], 0
        text = (data / "commits" / f"{cid}.patch").read_text()
        failed, code = run_tests(src, text, [num2id[n] for n in dropped], work / f"tree-{cid}")
        return cid, dropped, failed, code

    bad = 0
    with ThreadPoolExecutor(int(jobs)) as ex:
        for cid, dropped, failed, code in ex.map(check, sorted(meta)):
            miss = bool(failed) or code not in (0, 5)
            bad += miss
            tpc = [num2id[n] for n in dropped if "execute_tpc" in num2id[n]]
            print(f"{cid} {meta[cid]:55} old {len(sel_old[cid]):4} new {len(sel_new[cid]):4} ran {len(dropped):4} "
                  f"tpc dropped {len(tpc)} {'MISS ' + str(failed[:3]) + f' exit={code}' if miss else 'ok'}", flush=True)
    print(f"{len(meta)} commits, {bad} with a failing test the new version drops")


if __name__ == "__main__":
    main(*sys.argv[1:9])
