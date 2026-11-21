import ast
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_mutants import mutate_sql  # noqa: E402
from mutants import candidate_mutations, make_patch  # noqa: E402

SAMPLE_KINDS = (["boolop", "compare", "bool", "int", "other"], [4, 4, 4, 3, 1.5])
OTHER_KINDS = {"string", "drop-not", "arith", "delete-stmt"}


def sample_code_kinds(rng):
    kind = rng.choices(*SAMPLE_KINDS)[0]
    return OTHER_KINDS if kind == "other" else {kind}


def main(sqlglot_root, coverage_map, out_dir, count, seed):
    rng = random.Random(int(seed))
    root = Path(sqlglot_root)
    covered = {path: {int(n) for n in m} for path, m in json.loads(Path(coverage_map).read_text())["lines"].items()}
    files = sorted(p.relative_to(root).as_posix() for p in (root / "sqlglot").rglob("*.py"))
    fixtures = sorted(p.relative_to(root).as_posix() for p in (root / "tests" / "fixtures").rglob("*.sql"))
    cache = {}

    def code_edit(kinds):
        for _ in range(200):
            path = rng.choice(files)
            if path not in cache:
                text = (root / path).read_text()
                cache[path] = (text, candidate_mutations(path, text))
            text, cands = cache[path]
            pool = [c for c in cands if c[0] in kinds]
            hit = [c for c in pool if c[1] in covered.get(path, ())]
            pool = hit if hit and rng.random() < 0.85 else pool
            if not pool:
                continue
            kind, line, new = rng.choice(pool)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            return path, text, new, kind
        raise RuntimeError("no mutation found")

    def fixture_edit():
        for _ in range(50):
            path = rng.choice(fixtures)
            text = (root / path).read_text()
            new = mutate_sql(text, rng)
            if new and new != text:
                return path, text, new, "fixture"
        raise RuntimeError("no fixture mutation")

    out = Path(out_dir)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    n = int(count)
    plan = ["code"] * round(n * 0.55) + ["fixture"] * round(n * 0.25) + ["comment"] * round(n * 0.10)
    plan += ["multi"] * (n - len(plan))
    rng.shuffle(plan)
    for i, kind in enumerate(plan):
        if kind == "code":
            edits = [code_edit(sample_code_kinds(rng))]
        elif kind == "fixture":
            edits = [fixture_edit()]
        elif kind == "comment":
            edits = [code_edit({"comment"})]
        else:
            edits = [code_edit(sample_code_kinds(rng)), rng.choice([fixture_edit, lambda: code_edit({"comment"})])()]
            if edits[0][0] == edits[1][0]:
                edits = edits[:1]
        patch = "".join(make_patch(p, old, new) for p, old, new, _ in edits)
        cid = f"e{i:03d}"
        (out / "commits" / f"{cid}.patch").write_text(patch)
        meta[cid] = {"kind": kind, "edits": [k for *_, k in edits], "files": [p for p, *_ in edits], "file": edits[0][0], "line": 0}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {len(meta)} commits", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:6])
