"""Plants one-line edits for which the one-line lemma (blast/linelemma.py) narrows the
selection, and writes them out with the tests the lemma drops, so that those tests can
be run on the patched code. Run inside the selector's image (it reads the built map).

    python tools/line_lemma_bench.py <sqlglot dir> <tests.json> <out dir> <count> <seed>

Out: <out>/commits/<id>.patch, <out>/dropped.jsonl (one {"commit", "workers": [[dropped
test numbers]]} a line, the selector's output format), <out>/meta.json.
"""
import ast
import json
import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLAST = os.environ.get("BLAST_DIR", str(REPO / "blast"))
sys.path.insert(0, BLAST)
sys.path.insert(0, str(REPO / "tools" / "prover"))

import linelemma  # noqa: E402
import patchutil  # noqa: E402
from analyze import Analyzer, Maps  # noqa: E402
from mutants import candidate_mutations, make_patch  # noqa: E402


def main(src, tests_json, out, count, seed):
    rng = random.Random(int(seed))
    root = Path(src)
    tests = json.loads(Path(tests_json).read_text())["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    maps = Maps(os.path.join(BLAST, "covmap.pkl.gz"), src)
    to_number = [by_id.get(t) for t in maps.tests]
    off = Analyzer(maps, import_diff=True)
    on = Analyzer(maps, import_diff=True)
    on.line_lemma = True
    files = sorted(f.relative_to(root).as_posix() for f in (root / "sqlglot").rglob("*.py"))
    out = Path(out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta, rows, per_func, tried, held = {}, [], {}, 0, 0
    # a few edits from each file per pass, so that no file's candidates (each a whole
    # copy of the file) are kept around
    for _ in range(40):
        rng.shuffle(files)
        for path in files:
            if len(rows) >= int(count):
                break
            old = (root / path).read_text()
            tree = ast.parse(old)
            cands = candidate_mutations(path, old)
            for kind, line, new in rng.sample(cands, min(8, len(cands))):
                tried += 1
                try:
                    newt = ast.parse(new)
                except SyntaxError:
                    continue
                at = linelemma.one_line(path, tree, newt, old.split("\n"), new.split("\n"))
                if at is None:
                    continue
                held += 1
                patch = make_patch(path, old, new)
                files_ = patchutil.parse(patch)
                a, _ = off.analyze(patch, files_)
                b, notes = on.analyze(patch, files_)
                if a is None or b is None or not (a - b):
                    continue
                fn = next((n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and n.lineno <= at <= n.end_lineno), "?")
                if per_func.get((path, fn), 0) >= 2 or any(m["file"] == path and m["line"] == at for m in meta.values()):
                    continue
                per_func[(path, fn)] = per_func.get((path, fn), 0) + 1
                cid = f"m{len(rows):03d}"
                (out / "commits" / f"{cid}.patch").write_text(patch)
                dropped = sorted(to_number[i] for i in a - b if to_number[i] is not None)
                rows.append({"commit": cid, "workers": [dropped]})
                meta[cid] = {"file": path, "line": at, "kind": kind, "function": fn, "before": len(a), "after": len(b),
                             "note": notes[-1] if notes else ""}
            del cands
        if len(rows) >= int(count):
            break
    (out / "dropped.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"{tried} edits tried, lemma held on {held}, {len(rows)} narrowing edits written; "
          f"{sum(len(r['workers'][0]) for r in rows)} dropped tests to run", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
