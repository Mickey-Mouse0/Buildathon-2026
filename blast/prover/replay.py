import ast
import enum
import math
import operator

from targets import OPERATOR_NODES, leaves_of
from values import MISSING, Opaque

PURE_CALLS = {"isinstance", "issubclass", "len", "type", "bool", "callable", "hasattr"}
SINGLETON_TYPES = (type(None), bool)
NUMERIC = (int, float, complex)
ORDERED = (int, float, str, bytes)
MAX_SEQUENCE = 10_000


class Unknown(Exception):
    pass


def dump(node):
    return ast.dump(node, include_attributes=False)


def is_pure(leaf):
    for node in ast.walk(leaf):
        if isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda)):
            return False
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in PURE_CALLS):
                return False
    return True


def truth(value):
    if isinstance(value, Opaque):
        if value.truthy is None:
            raise Unknown("opaque truthiness")
        return value.truthy
    return bool(value)


def _is_singleton(value):
    return isinstance(value, SINGLETON_TYPES) or isinstance(value, (enum.Enum, type))


def identical(a, b):
    if isinstance(a, Opaque) and isinstance(b, Opaque):
        if a.ident is None or b.ident is None:
            raise Unknown("identity of value-compared opaque")
        return a.ident == b.ident
    if isinstance(a, Opaque) or isinstance(b, Opaque):
        return False
    if _is_singleton(a) or _is_singleton(b):
        return a is b
    raise Unknown("identity of non-singleton primitive")


def equal(a, b):
    if isinstance(a, Opaque) and isinstance(b, Opaque):
        if a.eqmode == "identity" and b.eqmode == "identity":
            return a.ident == b.ident
        if a.eqmode == "value" and b.eqmode == "value":
            return a.typename == b.typename and a.eqkey == b.eqkey
        raise Unknown("opaque equality")
    if isinstance(a, Opaque) or isinstance(b, Opaque):
        opaque = a if isinstance(a, Opaque) else b
        if opaque.eqmode in ("identity", "value"):
            return False
        raise Unknown("custom __eq__")
    return a == b


def contains(container, item):
    if isinstance(container, Opaque):
        raise Unknown("membership in opaque container")
    if isinstance(item, Opaque):
        if item.eqmode == "identity" and isinstance(container, (tuple, frozenset)):
            return False
        raise Unknown("opaque membership")
    return item in container


def compare(op, a, b):
    if isinstance(op, ast.Is):
        return identical(a, b)
    if isinstance(op, ast.IsNot):
        return not identical(a, b)
    if isinstance(op, ast.Eq):
        return equal(a, b)
    if isinstance(op, ast.NotEq):
        return not equal(a, b)
    if isinstance(op, ast.In):
        return contains(b, a)
    if isinstance(op, ast.NotIn):
        return not contains(b, a)
    if isinstance(a, ORDERED) and isinstance(b, ORDERED):
        ops = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
        try:
            return ops[type(op)](a, b)
        except TypeError as error:
            raise Unknown(str(error))
    raise Unknown(f"comparison {type(op).__name__}")


BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.LShift: operator.lshift, ast.RShift: operator.rshift, ast.BitOr: operator.or_,
    ast.BitXor: operator.xor, ast.BitAnd: operator.and_,
}


def binary(op, a, b):
    if isinstance(a, Opaque) or isinstance(b, Opaque):
        raise Unknown("arithmetic on opaque")
    if not isinstance(a, (*NUMERIC, str, bytes, tuple)) or not isinstance(b, (*NUMERIC, str, bytes, tuple)):
        raise Unknown("arithmetic on non-primitive")
    if isinstance(op, (ast.Pow, ast.LShift)) and isinstance(b, int) and abs(b) > 64:
        raise Unknown("large power")
    if isinstance(op, ast.Mult) and any(isinstance(x, (str, bytes, tuple)) for x in (a, b)):
        size = max(len(x) if isinstance(x, (str, bytes, tuple)) else abs(x) for x in (a, b) if not isinstance(x, (float, complex)))
        if size > MAX_SEQUENCE:
            raise Unknown("large sequence")
    try:
        return BINARY[type(op)](a, b)
    except Exception as error:
        raise Unknown(f"arithmetic raised {type(error).__name__}")


def unary(op, a):
    if isinstance(op, ast.Not):
        return not truth(a)
    if isinstance(a, Opaque) or not isinstance(a, NUMERIC):
        raise Unknown("unary on non-number")
    if isinstance(op, ast.USub):
        return -a
    if isinstance(op, ast.UAdd):
        return +a
    if isinstance(op, ast.Invert) and isinstance(a, int):
        return ~a
    raise Unknown("unary")


class Evaluation:
    def __init__(self, root, leaf_slots, record_leaves):
        self.slots = leaf_slots
        self.record = record_leaves
        self.impure_trace = []
        self.pure = {id(leaf): is_pure(leaf) for leaf in leaves_of(root)}

    def leaf(self, node):
        slot = self.slots[id(node)]
        value = self.record[slot] if slot < len(self.record) else MISSING
        if value is MISSING:
            raise Unknown("leaf not evaluated in original run")
        if not self.pure[id(node)]:
            self.impure_trace.append(slot)
        return value

    def run(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if id(node) in self.slots:
            return self.leaf(node)
        if isinstance(node, ast.BoolOp):
            value = None
            for child in node.values:
                value = self.run(child)
                if isinstance(node.op, ast.And) and not truth(value):
                    return value
                if isinstance(node.op, ast.Or) and truth(value):
                    return value
            return value
        if isinstance(node, ast.Compare):
            left = self.run(node.left)
            result = True
            for op, comparator in zip(node.ops, node.comparators):
                right = self.run(comparator)
                result = compare(op, left, right)
                if not result:
                    return result
                left = right
            return result
        if isinstance(node, ast.UnaryOp):
            return unary(node.op, self.run(node.operand))
        if isinstance(node, ast.BinOp):
            return binary(node.op, self.run(node.left), self.run(node.right))
        if isinstance(node, ast.IfExp):
            return self.run(node.body) if truth(self.run(node.test)) else self.run(node.orelse)
        raise Unknown(f"cannot evaluate {type(node).__name__}")


def same(a, b, condition):
    if condition:
        return truth(a) == truth(b)
    if isinstance(a, Opaque) or isinstance(b, Opaque):
        return isinstance(a, Opaque) and isinstance(b, Opaque) and a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    if _is_singleton(a):
        return a is b
    return a == b


def slot_map(old_root, new_root):
    old_slots = {}
    for i, leaf in enumerate(leaves_of(old_root)):
        old_slots.setdefault(dump(leaf), []).append(i)
    old_ids = {id(leaf): i for i, leaf in enumerate(leaves_of(old_root))}
    new_ids = {}
    for leaf in leaves_of(new_root):
        candidates = old_slots.get(dump(leaf))
        if not candidates:
            return None, None
        new_ids[id(leaf)] = candidates
    return old_ids, new_ids


def prove_expression(old_root, new_root, condition, records):
    old_ids, new_candidates = slot_map(old_root, new_root)
    if old_ids is None:
        return False, "new expression uses a leaf the old one did not have"
    for record_leaves, recorded_result in records:
        try:
            new_ids = {}
            for leaf_id, candidates in new_candidates.items():
                present = [c for c in candidates if c < len(record_leaves) and record_leaves[c] is not MISSING]
                chosen = present[0] if present else candidates[0]
                for other in present[1:]:
                    if not same(record_leaves[other], record_leaves[chosen], False):
                        raise Unknown("duplicate leaf took different values")
                new_ids[leaf_id] = chosen
            old_eval = Evaluation(old_root, old_ids, record_leaves)
            old_value = old_eval.run(old_root)
            if not same(old_value, recorded_result, condition):
                return False, "replay of the original disagrees with the recording"
            new_eval = Evaluation(new_root, new_ids, record_leaves)
            new_value = new_eval.run(new_root)
            if old_eval.impure_trace != new_eval.impure_trace:
                return False, "side-effecting leaves evaluated differently"
            if not same(old_value, new_value, condition):
                return False, "mutated expression changes a recorded value"
        except Unknown as error:
            return False, f"unknown: {error}"
    return True, f"equal on all {len(records)} recorded evaluations"
