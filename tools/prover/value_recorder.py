import builtins
import concurrent.futures
import os
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "blast" / "prover"))

from values import MISSING, encode  # noqa: E402

CAP = 512

_stack = []
_records = {}
_overflow = set()


def __bpb__(number):
    _stack.append((number, {}))


def __bpl__(number, position, value):
    for frame_number, leaves in reversed(_stack):
        if frame_number == number:
            leaves[position] = value
            break
    return value


def __bpe__(number, _, result):
    while _stack:
        frame_number, leaves = _stack.pop()
        if frame_number == number:
            break
    else:
        return result
    if number in _overflow:
        return result
    identities = {}
    size = max(leaves) + 1 if leaves else 0
    encoded = tuple(encode(leaves[i], identities) if i in leaves else MISSING for i in range(size))
    record = (encoded, encode(result, identities))
    seen = _records.setdefault(number, set())
    if record not in seen:
        if len(seen) >= CAP:
            _overflow.add(number)
            del _records[number]
        else:
            seen.add(record)
    return result


builtins.__bpb__ = __bpb__
builtins.__bpl__ = __bpl__
builtins.__bpe__ = __bpe__


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


def pytest_sessionfinish(session, exitstatus):
    out = os.environ["VALUE_PROFILE_OUT"]
    with open(out, "wb") as f:
        pickle.dump(
            {"records": _records, "overflow": sorted(_overflow), "exitstatus": int(exitstatus)},
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
