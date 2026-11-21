"""pytest plugin: what each test hands the tokenizer, and what runs or is read outside it.

Inside = during Tokenizer.__init__ or Tokenizer.tokenize (any thread's own depth).
For blast/tokreplay.py we record:
- every function code object that starts or resumes outside, as line ranges, so a
  patch to a line no such range holds only ever runs inside the tokenizer
  (import-time and collection-time runs count as outside);
- per test, every class setting read outside the tokenizer;
- per test, every outermost tokenize call as (tokenizer class, dialect fields, sql).
  Right after the call we tokenize again with a fresh tokenizer built from that
  description; if the results differ (or can't be compared), the test is marked
  not replayable and keeps its selection.

Each test runs in a fork of the post-import parent, like keymap_plugin.py.
"""
import concurrent.futures
import gc
import hashlib
import os
import pickle
import sys
import threading
from pathlib import Path

import pytest

BLAST = Path(__file__).resolve().parent.parent / "blast"
sys.path.insert(0, str(BLAST))

import tokreplay as tr  # noqa: E402
from importstate import _SKIP, _kind, import_all  # noqa: E402
from keymap_plugin import InlineExecutor, _watch_processes, _cur as _km_cur, _spawned  # noqa: E402

OUT = os.environ.get("TOKREPLAY_OUT")
CO_NEWLOCALS = 0x2
TOOL = 4
mon = sys.monitoring
_tl = threading.local()
_quiet = [0]
_rec = [None]
_collect = set()
_outside = set()
_wrapped = set()
_failed = set()
_PKG = [None]
_armed = [False]
# tokenizer calls made while test modules are imported, outside any test
_collect_calls = {"reads": set(), "calls": set(), "bad": None}


def _depth():
    return getattr(_tl, "d", 0)


def on_start(code, offset, _pkg=_PKG, _disable=mon.DISABLE):
    pkg = _pkg[0]
    if pkg is None or not code.co_filename.startswith(pkg):
        return _disable
    if getattr(_tl, "d", 0):
        return None
    if code.co_flags & CO_NEWLOCALS:
        _outside.add(code)
    return _disable


def _read(key):
    if getattr(_tl, "d", 0) or _quiet[0]:
        return
    r = _rec[0]
    if r is None:
        _collect.add(key)
    else:
        r["reads"].add(key)


class OutRec:
    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, obj, owner=None):
        _read(self.k)
        return self.v


def _outlog_class():
    def make(name):
        base = getattr(dict, name)

        def method(self, *a, **kw):
            for k in self._keys:
                _read(k)
            return base(self, *a, **kw)

        method.__name__ = name
        return method

    ns = {"__slots__": ("_keys",)}
    for name in ("__getitem__", "get", "__contains__", "__setitem__", "__delitem__", "setdefault", "pop", "__iter__",
                 "__reversed__", "keys", "__len__", "values", "items", "copy", "__eq__", "__ne__", "__or__", "__ror__",
                 "__ior__", "update", "clear", "popitem", "__repr__", "__sizeof__"):
        ns[name] = make(name)

    def __reduce__(self):
        for k in self._keys:
            _read(k)
        return (dict, (dict.copy(self),))

    ns["__reduce__"] = __reduce__
    ns["__reduce_ex__"] = lambda self, protocol: self.__reduce__()
    ns["__copy__"] = lambda self: self.copy()

    def __deepcopy__(self, memo):
        import copy

        for k in self._keys:
            _read(k)
        return copy.deepcopy(dict.copy(self), memo)

    ns["__deepcopy__"] = __deepcopy__
    return type("OutLog", (dict,), ns)


OutLog = _outlog_class()


def _settings():
    """Every plain class setting, as snapshot() in importstate walks classes."""
    import enum

    out = []
    for modname, mod in sorted(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if isinstance(val, type) and val.__module__ == modname and val.__qualname__ == name:
                stack = [val]
                while stack:
                    cls = stack.pop()
                    for an, av in list(cls.__dict__.items()):
                        if isinstance(av, type) and av.__module__ == modname and av.__qualname__ == f"{cls.__qualname__}.{an}":
                            stack.append(av)
                        if an in _SKIP or issubclass(cls, enum.Enum) or _kind(cls, an, av) != "plain":
                            continue
                        out.append((cls, an, av, f"{modname}:{cls.__qualname__}.{an}"))
    return out


def _wrap_settings():
    found = _settings()
    dicts = {}
    for cls, an, av, key in found:
        if type(av) is dict:
            dicts.setdefault(id(av), [av, []])[1].append((cls, an, key))
        else:
            try:
                type.__setattr__(cls, an, OutRec(av, key))
                _wrapped.add(key)
            except Exception:
                pass
    gc.collect()
    groups = list(dicts.values())
    mine = {id(found), id(dicts), id(groups), id(sys._getframe())} | {id(g) for g in groups}
    del found
    for d, owners in groups:
        allowed = {id(x) for cls, an, _ in owners for x in gc.get_referents(cls) if type(x) is dict and x.get(an) is d}
        if any(id(r) not in allowed and id(r) not in mine for r in gc.get_referrers(d)):
            continue
        ol = OutLog(d)
        ol._keys = tuple(k for _, _, k in owners)
        for cls, an, key in owners:
            type.__setattr__(cls, an, ol)
            _wrapped.add(key)


def _wrap_tokenizer():
    from sqlglot import tokens

    T = tokens.Tokenizer
    init, tokenize = T.__init__, T.tokenize

    def __init__(self, dialect=None):
        _tl.d = _depth() + 1
        try:
            init(self, dialect)
        finally:
            _tl.d -= 1

    def tokenize_(self, sql):
        _tl.d = _depth() + 1
        res = err = None
        try:
            res = tokenize(self, sql)
            return res
        except BaseException as e:
            err = e
            raise
        finally:
            _tl.d -= 1
            if _tl.d == 0 and not _quiet[0] and _armed[0]:
                _record(self, sql, res, err)

    T.__init__ = __init__
    T.tokenize = tokenize_


def _record(tok, sql, res, err):
    r = _rec[0] if _rec[0] is not None else _collect_calls
    _quiet[0] += 1
    try:
        desc = tr.describe_dialect(tok.dialect)
        cls = type(tok)
        key = f"{cls.__module__}:{cls.__qualname__}"
        if desc is None or not isinstance(sql, str):
            r["bad"] = "dialect fields not plain" if desc is None else "sql not a str"
            return
        seen = tr.outcome(tok, res, err)
        fresh = tr.build(key, desc[0], desc[1])
        ferr = fres = None
        try:
            fres = fresh.tokenize(sql)
        except Exception as e:
            ferr = e
        if tr.outcome(fresh, fres, ferr) != seen:
            r["bad"] = f"fresh tokenizer differs for {key}"
            return
        r["calls"].add((key, desc[0], desc[1], sql))
    except Exception as e:
        r["bad"] = f"recording failed: {e!r}"[:200]
    finally:
        _quiet[0] -= 1


def _ranges(codes):
    out = set()
    root = os.path.dirname(_PKG[0].rstrip(os.sep))
    for c in codes:
        ls = [l for _, _, l in c.co_lines() if l is not None]
        if ls:
            out.add((os.path.relpath(c.co_filename, root), min(ls), max(ls)))
    return out


def pytest_configure(config):
    if not OUT:
        return
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    _watch_processes()
    import importlib.util

    spec = importlib.util.find_spec("sqlglot")
    _PKG[0] = os.path.dirname(spec.origin) + os.sep
    # sqlglot tokenizes SQL while it is imported. What that leaves behind is the
    # import diff's business (the selector requires a clean one), so watching for
    # code run outside the tokenizer starts only after the import.
    fail = import_all()
    _wrap_tokenizer()
    _wrap_settings()
    _armed[0] = True
    mon.use_tool_id(TOOL, "tokreplay")
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.register_callback(TOOL, mon.events.PY_RESUME, on_start)
    mon.set_events(TOOL, mon.events.PY_START | mon.events.PY_RESUME)
    os.makedirs(OUT, exist_ok=True)
    print(f"tokreplay: {len(_wrapped)} settings watched, import failures {fail}", file=sys.stderr)


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    if not OUT:
        return None
    pid = os.fork()
    if pid == 0:
        try:
            from _pytest.runner import runtestprotocol

            before = set(_outside)
            _rec[0] = {"reads": set(), "calls": set(), "bad": None}
            _km_cur[0] = {}
            runtestprotocol(item, nextitem=None, log=True)
            r = _rec[0]
            _rec[0] = None
            name = hashlib.sha1(item.nodeid.encode()).hexdigest()
            with open(os.path.join(OUT, f"t-{name}.pkl"), "wb") as f:
                pickle.dump({"test": item.nodeid, "reads": r["reads"], "calls": r["calls"], "bad": r["bad"],
                             "spawned": _spawned[0], "failed": bool(_failed),
                             "outside": _ranges(_outside - before)}, f, protocol=pickle.HIGHEST_PROTOCOL)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    return True


def pytest_sessionfinish(session, exitstatus):
    if OUT:
        with open(os.path.join(OUT, f"parent-{os.getpid()}.pkl"), "wb") as f:
            pickle.dump({"outside": _ranges(_outside), "collect": _collect, "wrapped": _wrapped,
                         "collect_calls": _collect_calls["calls"], "collect_bad": _collect_calls["bad"]}, f)
