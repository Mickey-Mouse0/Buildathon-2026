import ast

OPERATOR_NODES = (ast.BoolOp, ast.Compare, ast.UnaryOp, ast.BinOp, ast.IfExp)
CONDITION_OWNERS = (ast.If, ast.While, ast.Assert)
UNSAFE_INSIDE = (ast.Yield, ast.YieldFrom, ast.Await)


def target_key(path, node):
    return f"{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"


def leaves_of(root):
    if not isinstance(root, OPERATOR_NODES):
        return [root]
    out = []

    def walk(node):
        for child in operator_children(node):
            if isinstance(child, OPERATOR_NODES):
                walk(child)
            elif not isinstance(child, ast.Constant):
                out.append(child)

    walk(root)
    return out


def operator_children(node):
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.Compare):
        return [node.left, *node.comparators]
    if isinstance(node, ast.UnaryOp):
        return [node.operand]
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if isinstance(node, ast.IfExp):
        return [node.test, node.body, node.orelse]
    return []


def annotation_nodes(tree):
    skip = set()
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None:
                    annotations.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            if annotation is not None:
                skip.update(id(n) for n in ast.walk(annotation))
    return skip


def condition_nodes(tree):
    conditions = set()
    for node in ast.walk(tree):
        if isinstance(node, CONDITION_OWNERS):
            conditions.add(id(node.test))
        elif isinstance(node, ast.IfExp):
            conditions.add(id(node.test))
        elif isinstance(node, ast.comprehension):
            conditions.update(id(c) for c in node.ifs)
    return conditions


def find_targets(tree):
    skip = annotation_nodes(tree)
    conditions = condition_nodes(tree)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    roots = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr) or id(node) in skip:
            continue
        parent = parents.get(id(node))
        is_operator_root = isinstance(node, OPERATOR_NODES) and not (
            isinstance(parent, OPERATOR_NODES) and any(c is node for c in operator_children(parent))
        )
        is_bare_condition = id(node) in conditions and not isinstance(node, OPERATOR_NODES)
        if is_bare_condition and isinstance(parent, OPERATOR_NODES):
            is_bare_condition = False
        if not (is_operator_root or is_bare_condition):
            continue
        if any(isinstance(n, UNSAFE_INSIDE) for n in ast.walk(node)):
            continue
        roots.append(node)
    return roots, conditions
