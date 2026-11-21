import builtins
import concurrent.futures
import io
import json
import os
import pickle

import pytest

_current = None
_opened = {}
_dialects = {}
_real_open = builtins.open
_real_io_open = io.open


def _record(path):
    if _current is None:
        return
    try:
        p = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    if "/tests/" in p:
        _opened.setdefault(_current, set()).add(p.split("/tests/", 1)[1])


def _open(file, *a, **k):
    _record(file)
    return _real_open(file, *a, **k)


def _io_open(file, *a, **k):
    _record(file)
    return _real_io_open(file, *a, **k)


builtins.open = _open
io.open = _io_open


# The suite fans fixture cases out to spawned process pools, which per-test
# coverage cannot see into, so pools run inline. Arguments and results are
# pickled anyway because crossing the process boundary exercises sqlglot.serde.
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


def _note_dialect(dialect_class):
    if _current is None:
        return
    seen = _dialects.setdefault(_current, set())
    for klass in dialect_class.__mro__:
        module = getattr(klass, "__module__", "")
        if module.startswith("sqlglot.dialects."):
            seen.add(module.rsplit(".", 1)[1])


def _instrument_dialects():
    from sqlglot.dialects import dialect as dialect_module
    from sqlglot.dialects.dialect import Dialect, Dialects

    for value in Dialects:
        if value.value:
            try:
                Dialect.get_or_raise(value.value)
            except Exception:
                pass

    original_init = Dialect.__init__

    def __init__(self, *args, **kwargs):
        _note_dialect(type(self))
        original_init(self, *args, **kwargs)

    Dialect.__init__ = __init__

    raw_get = dialect_module._Dialect.__dict__["__getitem__"]
    original_get = raw_get.__func__ if isinstance(raw_get, classmethod) else raw_get

    def __getitem__(cls, key):
        result = original_get(cls, key)
        _note_dialect(result)
        return result

    dialect_module._Dialect.__getitem__ = (
        classmethod(__getitem__) if isinstance(raw_get, classmethod) else __getitem__
    )

    original_get_or_raise = Dialect.get_or_raise.__func__

    def get_or_raise(cls, dialect):
        result = original_get_or_raise(cls, dialect)
        _note_dialect(type(result))
        return result

    Dialect.get_or_raise = classmethod(get_or_raise)


def pytest_collection_finish(session):
    _instrument_dialects()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current
    _current = item.nodeid
    yield
    _current = None
    out = os.environ["FIXTURE_TRACKER_OUT"]
    os.makedirs(out, exist_ok=True)
    with _real_open(os.path.join(out, f"{os.getpid()}.jsonl"), "a") as f:
        record = [
            item.nodeid,
            sorted(_opened.pop(item.nodeid, ())),
            sorted(_dialects.pop(item.nodeid, ())),
        ]
        f.write(json.dumps(record) + "\n")
