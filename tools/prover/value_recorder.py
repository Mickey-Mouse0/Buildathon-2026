import builtins
import concurrent.futures
import enum
import hashlib
import os
import pickle
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "blast" / "prover"))

from values import MISSING, encode  # noqa: E402

CAP = 512

_stack = []
_records = {}
_overflow = set()
_raised = set()
_MAIN = threading.main_thread().ident


def __bpb__(number):
    if threading.get_ident() != _MAIN:
        _raised.add(number)
        return
    _stack.append((number, {}, {}, {}, {}))


def __bpl__(number, position, value):
    if threading.get_ident() != _MAIN:
        _raised.add(number)
        return value
    for frame in reversed(_stack):
        if frame[0] == number:
            frame[1][position] = encode(value, frame[2])
            frame[3][position] = value
            break
    return value


def __bpe__(number, _, result):
    if threading.get_ident() != _MAIN:
        _raised.add(number)
        return result
    while _stack:
        frame_number, leaves, identities, _, relations = _stack.pop()
        if frame_number == number:
            break
        _raised.add(frame_number)
    else:
        _raised.add(number)
        return result
    if number in _overflow:
        return result
    size = max(leaves) + 1 if leaves else 0
    encoded = tuple(leaves.get(i, MISSING) for i in range(size))
    record = (encoded, encode(result, identities), tuple(sorted(relations.items())))
    seen = _records.setdefault(number, set())
    if record not in seen:
        if len(seen) >= CAP:
            _overflow.add(number)
            del _records[number]
        else:
            seen.add(record)
    return result


_SIMPLE = (type(None), bool, int, float, str, bytes)
_HEAVY_MODULES = ("pandas", "numpy")


def _is_simple(value):
    if isinstance(value, type):
        return type(value).__eq__ is type.__eq__
    if isinstance(value, enum.Enum):
        return type(value).__eq__ in (object.__eq__, str.__eq__, int.__eq__)
    return isinstance(value, _SIMPLE)


def _expression_class():
    core = sys.modules.get("sqlglot.expressions.core")
    return getattr(core, "Expr", None) if core else None


def _safe_eq(a, b):
    if a is b:
        return True
    expr = _expression_class()
    if expr is not None and isinstance(a, expr) and isinstance(b, expr):
        if type(a) is not type(b):
            return False
        # Expr caches its structural hash in _hash and clears it on every change,
        # so computing it here early cannot alter any later result.
        return hash(a) == hash(b)
    for value in (a, b):
        if type(value).__module__.split(".")[0] in _HEAVY_MODULES:
            return MISSING
    if _is_simple(a) or _is_simple(b):
        if expr is not None and (isinstance(a, expr) or isinstance(b, expr)):
            return False
        other = b if _is_simple(a) else a
        if _is_simple(other) or getattr(type(other), "__eq__", None) is object.__eq__:
            result = a == b
            return result if type(result) is bool else MISSING
    return MISSING


def _orderable(value):
    if isinstance(value, (int, float, str, bytes)):
        return True
    return isinstance(value, tuple) and len(value) <= 64 and all(isinstance(x, (int, float, str, bytes)) for x in value)


def _relate(family, a, b):
    if family == 0:
        eq = _safe_eq(a, b)
        ne = MISSING if eq is MISSING else not eq
        return (a is b, a is not b, eq, ne, MISSING, MISSING, MISSING, MISSING, MISSING, MISSING)
    if family == 1 and _orderable(a) and _orderable(b):
        try:
            return (a is b, a is not b, MISSING, MISSING, a < b, a <= b, a > b, a >= b, MISSING, MISSING)
        except TypeError:
            return None
    return None


def __bpr__(number, position, value, compare_index, left_slot, mode, const):
    __bpl__(number, position, value)
    if threading.get_ident() != _MAIN:
        return value
    family, placement = mode % 10, mode // 10
    for frame in reversed(_stack):
        if frame[0] == number:
            try:
                if placement == 1:
                    relation = _relate(family, value, const)
                elif placement == 2:
                    relation = _relate(family, const, value)
                elif left_slot in frame[3]:
                    relation = _relate(family, frame[3][left_slot], value)
                else:
                    relation = None
            except Exception:
                relation = None
            if relation is not None:
                frame[4][compare_index] = relation
            break
    return value


builtins.__bpb__ = __bpb__
builtins.__bpl__ = __bpl__
builtins.__bpe__ = __bpe__
builtins.__bpr__ = __bpr__


class InlineExecutor(concurrent.futures.Executor):
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as error:
            future.set_exception(error)
        return future


concurrent.futures.ProcessPoolExecutor = InlineExecutor


def pytest_collection_finish(session):
    from sqlglot.dialects.dialect import Dialect, Dialects

    for value in Dialects:
        if value.value:
            try:
                Dialect.get_or_raise(value.value)
            except Exception:
                pass


_failed_reports = set()
_FORK_DIR = os.environ.get("VALUE_PROFILE_FORK_DIR")
_SKIP = set()
if os.environ.get("VALUE_PROFILE_SKIP"):
    with open(os.environ["VALUE_PROFILE_SKIP"]) as f:
        _SKIP = set(f.read().split("\n")) - {""}


def pytest_runtest_logreport(report):
    if report.failed:
        _failed_reports.add(report.nodeid)


# Each test runs in a fork of the post-import parent, so class setup runs inside
# every test and no test sees state or warm caches left by another.
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    if not _FORK_DIR:
        return None
    if item.nodeid in _SKIP:
        return True
    pid = os.fork()
    if pid == 0:
        try:
            from _pytest.runner import runtestprotocol

            runtestprotocol(item, nextitem=None, log=True)
            _raised.update(frame[0] for frame in _stack)
            name = hashlib.sha1(item.nodeid.encode()).hexdigest()
            with open(os.path.join(_FORK_DIR, f"{name}.pkl"), "wb") as f:
                pickle.dump(
                    {"test": item.nodeid, "records": _records, "overflow": sorted(_overflow),
                     "raised": sorted(_raised), "exitstatus": 1 if _failed_reports else 0},
                    f, protocol=pickle.HIGHEST_PROTOCOL,
                )
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    return True


def pytest_sessionfinish(session, exitstatus):
    if _FORK_DIR:
        return
    out = os.environ["VALUE_PROFILE_OUT"]
    _raised.update(frame[0] for frame in _stack)
    with open(out, "wb") as f:
        pickle.dump(
            {"records": _records, "overflow": sorted(_overflow), "raised": sorted(_raised), "exitstatus": int(exitstatus)},
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
