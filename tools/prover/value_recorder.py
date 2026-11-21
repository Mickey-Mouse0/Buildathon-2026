import builtins
import concurrent.futures
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
    _stack.append((number, {}, {}))


def __bpl__(number, position, value):
    if threading.get_ident() != _MAIN:
        _raised.add(number)
        return value
    for frame_number, leaves, identities in reversed(_stack):
        if frame_number == number:
            leaves[position] = encode(value, identities)
            break
    return value


def __bpe__(number, _, result):
    if threading.get_ident() != _MAIN:
        _raised.add(number)
        return result
    while _stack:
        frame_number, leaves, identities = _stack.pop()
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
