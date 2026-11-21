import gzip
import json
import lzma
import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "blast" / "prover"))

UNION_CAP = 2048


def main(tests_json, fork_dirs, heavy_profiles_pkl, target_index, sqlglot_dir, out_path):
    sys.path.insert(1, sqlglot_dir)
    ids = {t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}
    index = json.loads(Path(target_index).read_text())
    file_of = {int(n): key.split(":", 1)[0] for n, key in index.items()}

    runs = defaultdict(int)
    failed = set()
    records = defaultdict(set)
    flagged = set()

    def absorb(test_id, profile):
        if profile["exitstatus"] != 0:
            failed.add(test_id)
            return
        runs[test_id] += 1
        flagged.update(profile["overflow"])
        flagged.update(profile.get("raised", ()))
        for number, recs in profile["records"].items():
            if number in flagged:
                continue
            bucket = records[number]
            bucket.update(recs)
            if len(bucket) > UNION_CAP:
                flagged.add(number)

    seeds = fork_dirs.split(",")
    for directory in seeds:
        for path in Path(directory).glob("*.pkl"):
            with open(path, "rb") as f:
                profile = pickle.load(f)
            absorb(profile["test"], profile)
    with open(heavy_profiles_pkl, "rb") as f:
        heavy = pickle.load(f)
    for test_id, profile in heavy.items():
        absorb(test_id, profile)
        runs[test_id] = len(seeds)

    profiled = sorted(t for t in ids if runs.get(t) == len(seeds) and t not in failed)
    per_file = defaultdict(dict)
    for number, recs in records.items():
        if number not in flagged:
            per_file[file_of[number]][number] = recs
    flagged_by_file = defaultdict(list)
    for number in flagged:
        flagged_by_file[file_of[number]].append(number)
    payload = {
        "profiled": profiled,
        "files": {
            path: pickle.dumps({"records": per_file.get(path, {}), "flagged": sorted(flagged_by_file.get(path, []))},
                               protocol=pickle.HIGHEST_PROTOCOL)
            for path in set(per_file) | set(flagged_by_file)
        },
    }
    with lzma.open(out_path, "wb", preset=9) as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"profiled {len(profiled)} of {len(ids)} tests; failed {len(failed)}; "
          f"expressions with records {len(records) - len(flagged & set(records))}, flagged {len(flagged)}; "
          f"{Path(out_path).stat().st_size / 1e6:.2f} MB", file=sys.stderr)
    missing = sorted(ids - set(profiled))
    print(f"not profiled: {missing[:10]}{' ...' if len(missing) > 10 else ''}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
