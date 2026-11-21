import ast
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "blast" / "prover"))

from targets import OPERATOR_NODES, find_targets, leaves_of, operator_children, target_key  # noqa: E402


def call(name, *args):
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=[])


def wrap_leaves(root, number):
    if not isinstance(root, OPERATOR_NODES):
        return
    index = {id(leaf): i for i, leaf in enumerate(leaves_of(root))}

    def walk(node):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for position, child in enumerate(value):
                    if not any(child is c for c in operator_children(node)):
                        continue
                    if isinstance(child, OPERATOR_NODES):
                        walk(child)
                    elif id(child) in index:
                        value[position] = call("__bpl__", ast.Constant(number), ast.Constant(index[id(child)]), child)
            elif isinstance(value, ast.expr) and any(value is c for c in operator_children(node)):
                if isinstance(value, OPERATOR_NODES):
                    walk(value)
                elif id(value) in index:
                    setattr(node, field, call("__bpl__", ast.Constant(number), ast.Constant(index[id(value)]), value))

    walk(root)


class Instrumenter(ast.NodeTransformer):
    def __init__(self, numbers):
        self.numbers = numbers

    def visit(self, node):
        node = self.generic_visit(node)
        number = self.numbers.get(id(node))
        if number is None:
            return node
        if isinstance(node, OPERATOR_NODES):
            wrap_leaves(node, number)
            body = node
        else:
            body = call("__bpl__", ast.Constant(number), ast.Constant(0), node)
        wrapped = call("__bpe__", ast.Constant(number), call("__bpb__", ast.Constant(number)), body)
        return ast.copy_location(wrapped, node)


def main(source_root, out_root, index_path):
    source_root = Path(source_root)
    out_root = Path(out_root)
    if out_root.exists():
        shutil.rmtree(out_root)
    shutil.copytree(source_root / "sqlglot", out_root / "sqlglot")
    index = {}
    next_number = 0
    for path in sorted((source_root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        roots, _ = find_targets(tree)
        numbers = {}
        for root in roots:
            numbers[id(root)] = next_number
            index[next_number] = target_key(rel, root)
            next_number += 1
        tree = Instrumenter(numbers).visit(tree)
        ast.fix_missing_locations(tree)
        (out_root / rel).write_text(ast.unparse(tree))
    Path(index_path).write_text(json.dumps(index))
    print(f"instrumented {next_number} expressions", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
