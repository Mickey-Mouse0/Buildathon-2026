import gzip
import json
import lzma
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "blast" / "prover"))

CAP = 512


def main(tests_json, line_map, profiles_dir, target_index, sqlglot_dir, out_dir):
    sys.path.insert(1, sqlglot_dir)
    tests = {t["number"]: t["id"] for t in json.loads(Path(tests_json).read_text())["tests"]}
    profiles = {}
    failed = set()
    for directory in profiles_dir.split(","):
        for path in sorted(Path(directory).glob("*.pkl")):
            test_id = tests[int(path.stem)]
            with open(path, "rb") as f:
                profile = pickle.load(f)
            if profile["exitstatus"] != 0:
                failed.add(test_id)
                continue
            merged = profiles.setdefault(test_id, {"records": {}, "overflow": set(), "raised": set(), "exitstatus": 0, "runs": 0})
            merged["runs"] += 1
            merged["overflow"].update(profile["overflow"])
            merged["raised"].update(profile.get("raised", ()))
            for number, records in profile["records"].items():
                merged["records"].setdefault(number, set()).update(records)
    runs = max((p["runs"] for p in profiles.values()), default=0)
    for test_id in list(profiles):
        merged = profiles[test_id]
        if test_id in failed or merged["runs"] < runs:
            del profiles[test_id]
            continue
        for number in [n for n, r in merged["records"].items() if len(r) > CAP]:
            merged["overflow"].add(number)
            del merged["records"][number]
        for number in merged["overflow"]:
            merged["records"].pop(number, None)
        merged["overflow"] = sorted(merged["overflow"])
        merged["raised"] = sorted(merged["raised"])
        del merged["runs"]

    heavy_numbers = sorted(n for n, i in tests.items() if i in profiles)
    position = {n: k for k, n in enumerate(heavy_numbers)}
    raw = json.loads(Path(line_map).read_text())

    def keep(numbers):
        return [position[n] for n in numbers if n in position]

    lines = {}
    for path, m in raw["lines"].items():
        kept = {line: keep(v) for line, v in m.items()}
        lines[path] = {line: v for line, v in kept.items() if v}
    heavy_map = {
        "tests": [tests[n] for n in heavy_numbers],
        "lines": lines,
        "import_lines": raw["import_lines"],
        "fixtures": {f: keep(v) for f, v in raw["fixtures"].items()},
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / "heavy_map.json.gz", "wt", compresslevel=9) as f:
        json.dump(heavy_map, f, separators=(",", ":"))
    with gzip.open(out / "target_index.json.gz", "wt", compresslevel=9) as f:
        f.write(Path(target_index).read_text())
    with lzma.open(out / "profiles.pkl.xz", "wb", preset=9) as f:
        pickle.dump(profiles, f, protocol=pickle.HIGHEST_PROTOCOL)
    for file in sorted(out.iterdir()):
        print(f"{file.name}: {file.stat().st_size / 1e6:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
