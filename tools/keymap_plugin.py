"""pytest plugin: which keys of each class-level dict every test reads.

Every plain dict stored as a sqlglot class attribute (Parser.FUNCTIONS,
Tokenizer.NUMERIC_ESCAPES, ...) is swapped for an equal KeyLog dict that records,
per test, the keys looked up through it, misses included. Whatever depends on more
than single keys is recorded as a pseudo key: KEYSET (iteration, len, bool) or ALL
(items, values, ==, copies, pickling).

Why one key is enough: a test's run can only diverge from the pinned commit's at a
read that returns something different. Until then it is the same run, so it reads
the same keys we recorded. A patch that changes key k (or the key order, or adds or
removes k) can therefore only affect tests that read k, KEYSET or ALL here.

A dict is left alone (no key-level data, so the selector keeps attribute-level
reads for it) when anything other than its own classes refers to it, since reads
through that reference would go to the original dict unseen.

Each test runs in a fork of the post-import, post-collection parent, so class setup
runs inside every test and no test sees caches warmed by another.

    KEYMAP_OUT=<dir> python -m pytest -p keymap_plugin ...   (see build_keymap.py)
"""
import concurrent.futures
import enum
import gc
import hashlib
import os
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blast"))

from importstate import ALL, KEYSET, UNKNOWN, dict_attrs, import_all, norm_key  # noqa: E402

OUT = os.environ.get("KEYMAP_OUT")
_KEYSET = object()
_ALL = object()
_BAD = object()
_cur = [None]
_collect = {}
_failed = set()
_groups = {}
_spawned = [False]


def _rec(gid, k):
    b = _cur[0]
    if b is None:
        b = _collect
    s = b.get(gid)
    if s is None:
        s = b[gid] = set()
    try:
        s.add(k)
    except Exception:
        s.add(_BAD)


class KeyLog(dict):
    __slots__ = ("_gid",)

    def __getitem__(self, k):
        _rec(self._gid, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _rec(self._gid, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _rec(self._gid, k)
        return dict.__contains__(self, k)

    def __setitem__(self, k, v):
        _rec(self._gid, k)
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        _rec(self._gid, k)
        dict.__delitem__(self, k)

    def setdefault(self, k, default=None):
        _rec(self._gid, k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *default):
        _rec(self._gid, k)
        return dict.pop(self, k, *default)

    def __iter__(self):
        _rec(self._gid, _KEYSET)
        return dict.__iter__(self)

    def __reversed__(self):
        _rec(self._gid, _KEYSET)
        return dict.__reversed__(self)

    def keys(self):
        _rec(self._gid, _KEYSET)
        return dict.keys(self)

    def __len__(self):
        _rec(self._gid, _KEYSET)
        return dict.__len__(self)

    def values(self):
        _rec(self._gid, _ALL)
        return dict.values(self)

    def items(self):
        _rec(self._gid, _ALL)
        return dict.items(self)

    def copy(self):
        _rec(self._gid, _ALL)
        return dict.copy(self)

    def __eq__(self, other):
        _rec(self._gid, _ALL)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _rec(self._gid, _ALL)
        return dict.__ne__(self, other)

    def __or__(self, other):
        _rec(self._gid, _ALL)
        return dict.__or__(self, other)

    def __ror__(self, other):
        _rec(self._gid, _ALL)
        return dict.__ror__(self, other)

    def __ior__(self, other):
        _rec(self._gid, _ALL)
        return dict.__ior__(self, other)

    def update(self, *a, **kw):
        _rec(self._gid, _ALL)
        dict.update(self, *a, **kw)

    def clear(self):
        _rec(self._gid, _ALL)
        dict.clear(self)

    def popitem(self):
        _rec(self._gid, _ALL)
        return dict.popitem(self)

    def __repr__(self):
        _rec(self._gid, _ALL)
        return dict.__repr__(self)

    def __sizeof__(self):
        _rec(self._gid, _ALL)
        return dict.__sizeof__(self)

    def __reduce__(self):
        _rec(self._gid, _ALL)
        return (dict, (dict.copy(self),))

    def __reduce_ex__(self, protocol):
        return self.__reduce__()

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        import copy

        _rec(self._gid, _ALL)
        return copy.deepcopy(dict.copy(self), memo)


class InlineExecutor(concurrent.futures.Executor):
    """ProcessPoolExecutor stand-in: pool work would otherwise run where we can't see it."""

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


def _watch_processes():
    """Reads in another process are invisible here, so a test that starts one is
    marked and keeps attribute-level selection (test_lazy_load runs its own python)."""
    import _posixsubprocess
    import subprocess

    def flag(fn):
        def inner(*a, **kw):
            if _cur[0] is not None:
                _spawned[0] = True
            return fn(*a, **kw)
        return inner

    subprocess._fork_exec = flag(subprocess._fork_exec)
    _posixsubprocess.fork_exec = flag(_posixsubprocess.fork_exec)
    for name in ("fork", "forkpty", "system", "posix_spawn", "posix_spawnp", "execv", "execve", "execl", "execle",
                 "execlp", "execlpe", "execvp", "execvpe", "spawnv", "spawnve", "spawnl", "spawnle"):
        if hasattr(os, name):
            setattr(os, name, flag(getattr(os, name)))


def _wrap():
    found = dict_attrs()
    by_id = {}
    for key, d in found.items():
        by_id.setdefault(id(d), [d, []])[1].append(key)
    unsafe = []
    gc.collect()
    groups = list(by_id.values())
    mine = {id(found), id(groups), id(sys._getframe())} | {id(g) for g in groups}
    for gid, (d, keys) in enumerate(groups):
        owners = []
        for _, qual, an in keys:
            mod, q = qual.split(":")
            obj = sys.modules[mod]
            for part in q.split("."):
                obj = getattr(obj, part)
            owners.append((obj, an))
        allowed = {id(x) for obj, an in owners for x in gc.get_referents(obj) if type(x) is dict and x.get(an) is d}
        outside = [r for r in gc.get_referrers(d) if id(r) not in allowed and id(r) not in mine]
        if outside:
            unsafe.extend(keys)
            continue
        kl = KeyLog(d)
        kl._gid = gid
        for obj, an in owners:
            type.__setattr__(obj, an, kl)
        _groups[gid] = keys
    del groups
    return unsafe


def pytest_configure(config):
    if not OUT:
        return
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    _watch_processes()
    fail = import_all()
    unsafe = _wrap()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"meta-{os.getpid()}.pkl"), "wb") as f:
        pickle.dump({"groups": _groups, "unsafe": unsafe, "import_fail": fail}, f)
    print(f"keymap: {len(_groups)} dicts wrapped, {len(unsafe)} left alone", file=sys.stderr)


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def _norm(bucket):
    out = {}
    for gid, ks in bucket.items():
        s = set()
        for k in ks:
            if k is _KEYSET:
                s.add(KEYSET)
            elif k is _ALL:
                s.add(ALL)
            elif k is _BAD:
                s.add(UNKNOWN)
            else:
                s.add(norm_key(k))
        out[tuple(_groups[gid])] = s
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    if not OUT:
        return None
    pid = os.fork()
    if pid == 0:
        try:
            from _pytest.runner import runtestprotocol

            _cur[0] = {}
            runtestprotocol(item, nextitem=None, log=True)
            reads = _norm(_cur[0])
            _cur[0] = None
            name = hashlib.sha1(item.nodeid.encode()).hexdigest()
            with open(os.path.join(OUT, f"t-{name}.pkl"), "wb") as f:
                pickle.dump({"test": item.nodeid, "reads": reads, "failed": bool(_failed), "spawned": _spawned[0]}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    return True


def pytest_sessionfinish(session, exitstatus):
    if OUT:
        with open(os.path.join(OUT, f"collect-{os.getpid()}.pkl"), "wb") as f:
            pickle.dump({"reads": _norm(_collect)}, f)
