import json
import sys
from collections import defaultdict
from pathlib import Path

HEAVY_MS = 300


def pack_cost(numbers, ms):
    if not numbers:
        return 0
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        for n in order:
            loads[loads.index(min(loads))] += ms[n]
        cost = max(loads) + 2000 * k
        best = cost if best is None else min(best, cost)
    return best


def main(bench_dir, output_jsonl, selector_log):
    bench = Path(bench_dir)
    tests = {t["number"]: t for t in json.loads((bench / "tests.json").read_text())["tests"]}
    ms = {n: round(t["seconds"] * 1000) for n, t in tests.items()}
    answers = json.loads((bench / "answers.json").read_text())["commits"]
    meta_path = bench / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {c: {"kind": a.get("kind", "?"), "edits": [a.get("kind", "?")]} for c, a in answers.items()}
    notes = defaultdict(list)
    for line in Path(selector_log).read_text().splitlines():
        cid = line.split(" ", 1)[0]
        if cid in answers:
            notes[cid].append(line.split(" ", 1)[1][:150] if " " in line else "")
    rows, buckets, misses = [], defaultdict(float), []
    for raw in Path(output_jsonl).read_text().splitlines():
        out = json.loads(raw)
        cid = out["commit"]
        selected = {n for w in out["workers"] for n in w}
        failing = set(answers[cid]["failing"])
        mine = pack_cost(selected, ms)
        best = pack_cost(failing, ms)
        if failing - selected:
            misses.append((cid, sorted(failing - selected)[:5]))
        gap = (mine - best) / 1000
        if gap <= 0:
            continue
        passing_heavy = [n for n in selected - failing if ms[n] >= HEAVY_MS]
        if len(selected) >= 0.9 * len(tests):
            cause = "ran everything"
        elif not failing:
            cause = "breaks nothing, still selected" + (" (incl. heavy)" if passing_heavy else "")
        elif pack_cost(selected - set(passing_heavy), ms) < mine:
            cause = "kept passing heavy tests"
        else:
            cause = "extra cheap tests / workers"
        buckets[cause] += gap
        heavy_names = [tests[n]["id"].split("::")[-1] for n in sorted(passing_heavy, key=lambda n: -ms[n])[:3]]
        rows.append((gap, cid, meta[cid]["kind"], "+".join(meta[cid]["edits"]), len(failing), len(selected), cause, heavy_names, notes[cid][:1]))
    total_mine = sum(pack_cost({n for w in json.loads(r)["workers"] for n in w}, ms) for r in Path(output_jsonl).read_text().splitlines()) / 1000
    total_best = sum(pack_cost(set(a["failing"]), ms) for a in answers.values()) / 1000
    print(f"{len(answers)} commits: ours {total_mine:.1f}s + {300 * len(misses)}s misses, perfect {total_best:.1f}s; "
          f"per commit ours {total_mine / len(answers):.2f}s vs perfect {total_best / len(answers):.2f}s")
    for cid, missed in misses:
        print(f"MISS {cid} {meta[cid]} missed tests {[tests[n]['id'] for n in missed]}")
    print("\nwaste by cause:")
    for cause, secs in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"  {secs:7.1f}s  {cause}")
    print("\nper commit:")
    for gap, cid, kind, edits, nfail, nsel, cause, heavy, note in sorted(rows, reverse=True):
        print(f"  {gap:6.2f}s {cid} {kind:7} {edits:20} fail={nfail:<4} sel={nsel:<5} {cause:35} {heavy} {note}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
