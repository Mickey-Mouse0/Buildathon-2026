"""The one-line lemma: when a patch replaces one line inside a multi-line statement and
the compiled code differs only in instructions of that line, the tests that can see
the change are those that ran that line, not every test that ran the statement.

Why it holds. The pinned and the patched function are compared instruction by
instruction (same count, same offsets, same line for every instruction, same jump
targets, names and constants). When they differ only at instructions the pinned code
has on line L, the two run exactly alike until one of those is reached, and reaching
one means an instruction of line L is executing in that function. The coverage map
(tools/covmap_plugin.py, sys.monitoring LINE events re-armed for every test) records
line L for a test whenever control enters L from another line in a frame of that
code. A frame can only be on L without having entered it when it starts there, so
a function whose first instructions are on L is refused, and so is a generator or
coroutine (its frame can outlive the test that entered the line). A test with no
record of line L therefore never reaches a differing instruction and runs exactly as
it did at the pinned commit.

What an `and` to `or` flip on line 3 of a condition does to line 1 is still covered:
such a flip changes jump instructions that belong to other lines, the comparison finds
them, and the statement keeps all of its tests. The same goes for a constant folded
into a tuple that starts on another line, for an edit that adds or removes
instructions, and for anything that changes a class body, a default or a decorator.
"""
import ast
import dis
import types

_GEN = 0x20 | 0x80 | 0x200  # generator, coroutine, async generator
_NEWLOCALS = 0x2


def _future_flags(tree):
    import __future__

    flags = 0
    for st in tree.body:
        if isinstance(st, ast.ImportFrom) and st.module == "__future__":
            for a in st.names:
                f = getattr(__future__, a.name, None)
                if f is not None:
                    flags |= f.compiler_flag
    return flags


def _span(st):
    start = st.decorator_list[0].lineno if getattr(st, "decorator_list", None) else st.lineno
    return start, st.end_lineno, st.col_offset, st.end_col_offset


def _const_key(v):
    return (type(v).__name__, repr(v))


def _handlers(code, index_of):
    """The exception table with offsets turned into instruction indexes."""
    out = []
    for e in dis._parse_exception_table(code):
        out.append((index_of.get(e.start), index_of.get(e.end, len(index_of)), index_of.get(e.target), e.depth, e.lasti))
    return out


def _instructions(code):
    """The code's instructions without EXTENDED_ARG prefixes (dis folds them into the
    argument of the instruction they belong to), and {offset: position in that list},
    where a prefix's offset stands for its instruction."""
    out, at, pending = [], {}, []
    for x in dis.get_instructions(code):
        if x.opname == "EXTENDED_ARG":
            pending.append(x.offset)
            continue
        for off in pending:
            at[off] = len(out)
        pending = []
        at[x.offset] = len(out)
        out.append(x)
    return out, at


def _differing(a, b, line, found):
    """Compare two code objects built from the pinned and the patched statement.
    Appends (code, instruction line) for each instruction that differs; False when
    the two can't be lined up instruction by instruction."""
    if a == b:
        return True
    for f in ("co_name", "co_qualname", "co_flags", "co_argcount", "co_kwonlyargcount", "co_posonlyargcount",
              "co_varnames", "co_freevars", "co_cellvars", "co_firstlineno"):
        if getattr(a, f) != getattr(b, f):
            return False
    subs_a = [c for c in a.co_consts if isinstance(c, types.CodeType)]
    subs_b = [c for c in b.co_consts if isinstance(c, types.CodeType)]
    if len(subs_a) != len(subs_b):
        return False
    pos_a = {id(c): i for i, c in enumerate(subs_a)}
    pos_b = {id(c): i for i, c in enumerate(subs_b)}
    # An instruction's size depends on its opcode (inline caches) and on how large its
    # argument is (EXTENDED_ARG prefixes), so offsets may shift after a changed one;
    # what a jump or a handler means is the instruction it lands on, so targets are
    # compared by position in the list.
    ia, at_a = _instructions(a)
    ib, at_b = _instructions(b)
    if len(ia) != len(ib):
        return False
    if _handlers(a, at_a) != _handlers(b, at_b):
        return False
    here = False
    for x, y in zip(ia, ib):
        if x.positions.lineno != y.positions.lineno:
            return False
        if x.opcode == y.opcode:
            xv, yv = x.argval, y.argval
            xc, yc = isinstance(xv, types.CodeType), isinstance(yv, types.CodeType)
            if xc or yc:
                # nested code is compared on its own below; here only that both
                # load the function at the same place in the list
                if xc and yc and pos_a.get(id(xv)) is not None and pos_a.get(id(xv)) == pos_b.get(id(yv)):
                    continue
                return False
            if x.opcode in _JUMPS:
                ta, tb = at_a.get(xv), at_b.get(yv)
                if ta is None or tb is None:
                    return False
                if ta == tb:
                    continue
            elif _const_key(xv) == _const_key(yv) and (x.arg == y.arg or x.opcode in _BY_VALUE):
                continue  # for a constant: the same one, maybe at another index of co_consts
        elif x.opcode in _JUMPS or y.opcode in _JUMPS:
            if (x.opcode in _JUMPS and at_a.get(x.argval) is None) or (y.opcode in _JUMPS and at_b.get(y.argval) is None):
                return False
        found.append((a, x.positions.lineno))
        here = True
    if here:
        if not (a.co_flags & _NEWLOCALS) or a.co_flags & _GEN:
            return False
        # the frame must enter the line from another one: not when it starts on it
        for x in ia:
            if x.positions.lineno == line:
                return False
            if x.opname == "RESUME":
                break
    return all(_differing(x, y, line, found) for x, y in zip(subs_a, subs_b))


# Instructions whose argument is nothing but an index into co_consts, so the same
# constant at another index means the same thing. (Name indexes are packed with flag
# bits in several instructions; those must match exactly.)
_BY_VALUE = frozenset(dis.hasconst)
_JUMPS = frozenset(dis.hasjrel) | frozenset(dis.hasjabs)


def one_line(path, old_tree, new_tree, old_lines, new_lines):
    """The line number L when the lemma above holds for this file's change: the tests
    that can see the change are exactly those the map lists for line L. None otherwise."""
    if len(old_lines) != len(new_lines):
        return None
    changed = [i for i, (x, y) in enumerate(zip(old_lines, new_lines)) if x != y]
    if len(changed) != 1:
        return None
    line = changed[0] + 1
    if len(old_tree.body) != len(new_tree.body) or old_tree.type_ignores or new_tree.type_ignores:
        return None
    at = None
    for i, (x, y) in enumerate(zip(old_tree.body, new_tree.body)):
        # every top-level statement sits where it did, so the ones without the line
        # are the same text at the same place and compile the same
        sx, sy = _span(x), _span(y)
        if type(x) is not type(y) or sx[:3] != sy[:3]:
            return None
        if sx[0] <= line <= sx[1]:
            if at is not None:
                return None
            at = i  # its last line may be the changed one, and end in another column
        elif sx != sy:
            return None
    if at is None or not isinstance(old_tree.body[at], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    flags = _future_flags(old_tree)
    if flags != _future_flags(new_tree):
        return None
    try:
        a = compile(ast.Module(body=[old_tree.body[at]], type_ignores=[]), path, "exec", flags=flags, dont_inherit=True)
        b = compile(ast.Module(body=[new_tree.body[at]], type_ignores=[]), path, "exec", flags=flags, dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = []
    if not _differing(a, b, line, found) or not found:
        return None
    if any(ln != line for _, ln in found):
        return None
    return line
