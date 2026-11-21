import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutants import make_patch  # noqa: E402


def mutate_sql(text, rng):
    lines = text.splitlines(keepends=True)
    candidates = [i for i, line in enumerate(lines) if re.search(r"\b(SELECT|WHERE|AND|=|<|>)\b|[=<>]", line) and not line.lstrip().startswith(("--", "#"))]
    if not candidates:
        return None
    i = rng.choice(candidates)
    line = lines[i]
    for old, new in ((" = ", " <> "), (" < ", " >= "), (" > ", " <= "), (" AND ", " OR "), ("SELECT ", "SELECT DISTINCT "), (" 1", " 2")):
        if old in line:
            lines[i] = line.replace(old, new, 1)
            return "".join(lines)
    lines[i] = line.rstrip("\n") + " x\n"
    return "".join(lines)


def mutate_python_test(text, rng):
    matches = [m for m in re.finditer(r'(?<![\w.])"([A-Za-z_][A-Za-z_ ]{2,20})"', text)]
    if not matches:
        return None
    m = rng.choice(matches)
    return text[: m.start(1)] + m.group(1) + "X" + text[m.end(1):]


def main(sqlglot_root, out_dir, count, seed):
    rng = random.Random(int(seed))
    root = Path(sqlglot_root)
    fixtures = sorted(p.relative_to(root).as_posix() for p in (root / "tests" / "fixtures").rglob("*.sql"))
    hot = [f for f in fixtures if "tpc-" in f or f.endswith(("identity.sql", "optimizer.sql", "simplify.sql", "qualify_columns.sql", "annotate_functions.sql", "merge_subqueries.sql", "canonicalize.sql", "unnest_subqueries.sql"))]
    shared_tests = ["tests/dialects/test_dialect.py", "tests/helpers.py"]
    out = Path(out_dir)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    attempts = 0
    while len(meta) < int(count) and attempts < int(count) * 20:
        attempts += 1
        roll = rng.random()
        if roll < 0.2:
            path, kind = rng.choice(shared_tests), "test-code"
            new = mutate_python_test((root / path).read_text(), rng)
        else:
            path, kind = rng.choice(hot if roll < 0.8 else fixtures), "fixture"
            new = mutate_sql((root / path).read_text(), rng)
        old = (root / path).read_text()
        if new is None or new == old:
            continue
        mid = f"d{len(meta):03d}"
        (out / "commits" / f"{mid}.patch").write_text(make_patch(path, old, new))
        meta[mid] = {"kind": kind, "file": path, "line": 0, "files": [path]}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {len(meta)} data/test mutants", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:5])
