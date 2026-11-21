import glob
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData

coverage_file, fixtures_dir, tests_json, sqlglot_root, out_path = sys.argv[1:6]

tests = json.load(open(tests_json))["tests"]
number_by_id = {t["id"]: t["number"] for t in tests}
numbers_by_class = defaultdict(set)
for t in tests:
    numbers_by_class[t["id"].rsplit("::", 1)[0]].add(t["number"])

data = CoverageData(coverage_file)
data.read()

root = os.path.realpath(sqlglot_root) + "/"
lines_map = {}
import_lines = {}
unknown_contexts = set()

for measured in data.measured_files():
    rel = os.path.realpath(measured).removeprefix(root)
    per_line = {}
    imports = []
    for line, contexts in data.contexts_by_lineno(measured).items():
        selected = set()
        for context in contexts:
            if not context:
                imports.append(line)
                continue
            nodeid, _, phase = context.rpartition("|")
            number = number_by_id.get(nodeid)
            if number is None:
                unknown_contexts.add(nodeid)
                continue
            if phase == "run":
                selected.add(number)
            else:
                selected |= numbers_by_class[nodeid.rsplit("::", 1)[0]]
        if selected:
            per_line[line] = sorted(selected)
    lines_map[rel] = per_line
    import_lines[rel] = sorted(set(imports))

fixture_map = defaultdict(set)
dialect_users = defaultdict(set)
for path in glob.glob(os.path.join(fixtures_dir, "*.jsonl")):
    for raw in open(path):
        nodeid, opened, dialects = json.loads(raw)
        number = number_by_id.get(nodeid)
        if number is None:
            continue
        for dialect in dialects:
            dialect_users[dialect].add(number)
        for fixture in opened:
            readers = {number}
            if fixture.endswith(".csv.gz"):
                readers = numbers_by_class[nodeid.rsplit("::", 1)[0]]
            fixture_map["tests/" + fixture] |= readers

print(f"unknown contexts: {len(unknown_contexts)}", sorted(unknown_contexts)[:5], file=sys.stderr)
json.dump(
    {
        "lines": lines_map,
        "import_lines": import_lines,
        "fixtures": {k: sorted(v) for k, v in fixture_map.items()},
        "dialect_users": {k: sorted(v) for k, v in dialect_users.items()},
    },
    open(out_path, "w"),
    separators=(",", ":"),
)
print(f"wrote {out_path}: {os.path.getsize(out_path) / 1e6:.1f} MB", file=sys.stderr)
