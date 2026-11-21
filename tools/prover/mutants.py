import ast
import difflib
import json
import random
import re
import sys
from pathlib import Path

COMPARE_SWAPS = {
    ast.Eq: [("==", "!=")], ast.NotEq: [("!=", "==")], ast.Is: [(" is ", " == "), (" is ", " is not ")],
    ast.IsNot: [(" is not ", " != "), (" is not ", " is ")], ast.Lt: [("<", "<=")], ast.LtE: [("<=", "<")],
    ast.Gt: [(">", ">=")], ast.GtE: [(">=", ">")], ast.In: [(" in ", " not in ")], ast.NotIn: [(" not in ", " in ")],
}


class Source:
    def __init__(self, text):
        self.text = text
        self.starts = [0]
        for line in text.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))

    def offset(self, line, col):
        line_text = self.text[self.starts[line - 1]:self.starts[line]]
        return self.starts[line - 1] + len(line_text.encode("utf-8")[:col].decode("utf-8", "ignore"))

    def replace(self, start, end, new):
        return self.text[:start] + new + self.text[end:]


def annotation_ids(tree):
    skip = set()
    for node in ast.walk(tree):
        parts = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.returns)
            a = node.args
            parts += [x.annotation for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x is not None]
        elif isinstance(node, ast.AnnAssign):
            parts.append(node.annotation)
        for p in parts:
            if p is not None:
                skip.update(id(n) for n in ast.walk(p))
    return skip


def candidate_mutations(path, text):
    tree = ast.parse(text)
    src = Source(text)
    skip = annotation_ids(tree)
    out = []

    def gap_swap(node, left, right, old, new, kind):
        a = src.offset(left.end_lineno, left.end_col_offset)
        b = src.offset(right.lineno, right.col_offset)
        gap = text[a:b]
        i = gap.find(old)
        if i < 0 or "#" in gap:
            return
        out.append((kind, node.lineno, src.replace(a + i, a + i + len(old), new)))

    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.BoolOp):
            old, new = (" and ", " or ") if isinstance(node.op, ast.And) else (" or ", " and ")
            gap_swap(node, node.values[0], node.values[1], old, new, "boolop")
        elif isinstance(node, ast.Compare):
            for swap_old, swap_new in COMPARE_SWAPS.get(type(node.ops[0]), []):
                gap_swap(node, node.left, node.comparators[0], swap_old, swap_new, "compare")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            old, new = (" + ", " - ") if isinstance(node.op, ast.Add) else (" - ", " + ")
            gap_swap(node, node.left, node.right, old, new, "arith")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            a = src.offset(node.lineno, node.col_offset)
            b = src.offset(node.operand.lineno, node.operand.col_offset)
            if text[a:b].strip() == "not":
                out.append(("drop-not", node.lineno, src.replace(a, b, "")))
        elif isinstance(node, ast.Constant) and node.lineno == node.end_lineno:
            a = src.offset(node.lineno, node.col_offset)
            b = src.offset(node.end_lineno, node.end_col_offset)
            literal = text[a:b]
            if isinstance(node.value, bool):
                out.append(("bool", node.lineno, src.replace(a, b, "False" if node.value else "True")))
            elif isinstance(node.value, int) and re.fullmatch(r"\d+", literal):
                out.append(("int", node.lineno, src.replace(a, b, str(node.value + 1))))
            elif isinstance(node.value, str) and re.fullmatch(r"[\"'][A-Za-z_ ]{1,30}[\"']", literal):
                out.append(("string", node.lineno, src.replace(a + 1, a + 1, "X")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                d = body[0].value
                b = src.offset(d.end_lineno, d.end_col_offset)
                quote = text[b - 3:b] if text[b - 3:b] in ('"""', "'''") else text[b - 1:b]
                out.append(("docstring", d.lineno, src.replace(b - len(quote), b - len(quote), " Extra.")))
            for stmt in body:
                if isinstance(stmt, (ast.Assign, ast.Return, ast.Expr, ast.AugAssign)) and stmt.lineno == stmt.end_lineno:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                        continue
                    line_start = src.starts[stmt.lineno - 1]
                    line_end = src.starts[stmt.lineno]
                    indent = re.match(r"\s*", text[line_start:line_end]).group(0)
                    out.append(("comment", stmt.lineno, src.replace(line_start, line_start, f"{indent}# Note\n")))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(body) > 1:
                        a = src.offset(stmt.lineno, stmt.col_offset)
                        b = src.offset(stmt.end_lineno, stmt.end_col_offset)
                        out.append(("delete-stmt", stmt.lineno, src.replace(a, b, "pass")))
    return out


def make_patch(path, old, new):
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3)
    return f"diff --git a/{path} b/{path}\n" + "".join(lines)


def main(sqlglot_root, coverage_map, profiles_dir, out_dir, count, seed):
    rng = random.Random(int(seed))
    root = Path(sqlglot_root)
    heavy = {int(p.stem) for p in Path(profiles_dir).glob("*.pkl")}
    lines = json.loads(Path(coverage_map).read_text())["lines"]
    hot = {}
    for path, m in lines.items():
        hot[path] = {int(n) for n, tests in m.items() if heavy & set(tests)}
    files = sorted(p.relative_to(root).as_posix() for p in (root / "sqlglot").rglob("*.py"))
    out = Path(out_dir)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    cache = {}
    meta = {}
    attempts = 0
    while len(meta) < int(count) and attempts < int(count) * 50:
        attempts += 1
        path = rng.choice(files)
        if path not in cache:
            text = (root / path).read_text()
            cache[path] = (text, candidate_mutations(path, text))
        text, candidates = cache[path]
        if not candidates:
            continue
        hot_candidates = [c for c in candidates if c[1] in hot.get(path, ())]
        pool = hot_candidates if hot_candidates and rng.random() < 0.7 else candidates
        kind, line, new = rng.choice(pool)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        mid = f"m{len(meta):04d}"
        (out / "commits" / f"{mid}.patch").write_text(make_patch(path, text, new))
        meta[mid] = {"kind": kind, "file": path, "line": line, "hot": line in hot.get(path, ())}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    kinds = {}
    for m in meta.values():
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    print(f"wrote {len(meta)} mutants: {kinds}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:7])
