"""pytest plugin: per-test line coverage of sqlglot + fixture files opened.

Uses sys.monitoring LINE events with DISABLE, re-armed per test via restart_events,
so each line costs one callback per test. Lines in module/class bodies (no
CO_NEWLOCALS) go into a global 'import' bucket.
Output: $COVMAP_OUT/<worker>.json
"""
import builtins, io, json, os, sys, importlib, pkgutil
import pytest

ROOT = os.environ.get("COVMAP_ROOT", os.getcwd())
PKG = os.path.join(ROOT, "sqlglot") + os.sep
TESTS = os.path.join(ROOT, "tests") + os.sep
OUT = os.environ["COVMAP_OUT"]
CO_NEWLOCALS = 0x2
mon = sys.monitoring
TOOL = 3  # free slot

state = {"cur": None}
per_test = {}
opened = {}
attrs = {}
mro = {}
importtime = set()
class_bucket = {}
_open = builtins.open
_io_open = io.open

def _rel(p):
    return p[len(ROOT) + 1:]

def on_line(code, line):
    fn = code.co_filename
    if not (fn.startswith(PKG) or fn.startswith(TESTS)):
        return mon.DISABLE
    key = (_rel(fn), line)
    if not (code.co_flags & CO_NEWLOCALS):
        importtime.add(key)
        return mon.DISABLE
    cur = state["cur"]
    if cur is not None:
        cur.add(key)
    return mon.DISABLE

class Rec:
    """Wraps a plain class attribute so reads are recorded per test."""
    __slots__ = ("v", "k")
    def __init__(self, v, k):
        self.v = v
        self.k = k
    def __get__(self, obj, owner=None):
        cur = state.get("attrs")
        if cur is not None:
            cur.add(self.k)
        return self.v

def wrap_class_attrs():
    import enum, types
    n = 0
    for modname, mod in list(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")):
            continue
        for obj in list(vars(mod).values()):
            if not isinstance(obj, type) or obj.__module__ != modname:
                continue
            stack = [obj]
            while stack:
                cls = stack.pop()
                mro[f"{modname}:{cls.__qualname__}"] = [f"{b.__module__}:{b.__qualname__}" for b in cls.__mro__[1:] if b.__module__.startswith("sqlglot")]
                if issubclass(cls, enum.Enum):
                    continue
                for name, val in list(cls.__dict__.items()):
                    if isinstance(val, type) and val.__module__ == modname and val.__qualname__.startswith(cls.__qualname__ + "."):
                        stack.append(val)
                    if name.startswith("__") or isinstance(val, Rec):
                        continue
                    if callable(val) and not isinstance(val, type) or hasattr(type(val), "__get__"):
                        continue
                    try:
                        setattr(cls, name, Rec(val, f"{modname}:{cls.__qualname__}.{name}"))
                        n += 1
                    except Exception:
                        pass
    print("covmap: wrapped", n, "class attrs", file=sys.stderr)

def rec_open(file, *a, **k):
    cur = state.get("opened")
    if cur is not None and isinstance(file, (str, bytes, os.PathLike)):
        p = os.path.abspath(os.fsdecode(file))
        if p.startswith(ROOT):
            cur.add(_rel(p))
    return _open(file, *a, **k)

class InlineExecutor:
    """Stand-in for ProcessPoolExecutor so pool work is traced in this process."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))
    def submit(self, fn, *a, **k):
        import concurrent.futures as cf
        f = cf.Future()
        try:
            import pickle
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return map(fn, *its)
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass

def pytest_configure(config):
    import concurrent.futures
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    mon.use_tool_id(TOOL, "covmap")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)
    builtins.open = rec_open
    io.open = rec_open
    state["cur"] = importtime  # everything run while collecting counts as import time

def pytest_collection_finish(session):
    # import every sqlglot module up front so lazy import work lands in 'import'
    import sqlglot
    state["cur"] = importtime
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:
            print("covmap: import fail", m.name, e, file=sys.stderr)
    state["cur"] = None
    if os.environ.get("COVMAP_ATTRS", "1") == "1":
        wrap_class_attrs()
    snapshot_globals()


_snapshots = []


def snapshot_globals():
    """Remember every module-level dict/list/set in sqlglot as it is after import.

    Some of them are per-process caches (generator._DISPATCH_CACHE). Without a reset,
    the code that fills a cache is only recorded for the first test that runs it, so a
    change there would map to one test instead of every test that relies on it.
    """
    for modname, mod in list(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")):
            continue
        for name, val in list(vars(mod).items()):
            if type(val) in (dict, list, set):
                _snapshots.append((val, val.copy()))
    print("covmap: snapshotted", len(_snapshots), "module containers", file=sys.stderr)


def reset_globals():
    for val, saved in _snapshots:
        if len(val) != len(saved):
            if isinstance(val, list):
                val[:] = saved
            else:
                val.clear()
                val.update(saved)

@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope == "function":
        yield
        return
    key = request.node.nodeid if fixturedef.scope == "class" else "__" + fixturedef.scope
    prev, prevo, preva = state["cur"], state.get("opened"), state.get("attrs")
    b = class_bucket.setdefault(key, [set(), set(), set()])
    state["cur"], state["opened"], state["attrs"] = b[0], b[1], b[2]
    mon.restart_events()
    try:
        yield
    finally:
        state["cur"], state["opened"], state["attrs"] = prev, prevo, preva
        mon.restart_events()

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    reset_globals()
    s, o, a = set(), set(), set()
    state["cur"], state["opened"], state["attrs"] = s, o, a
    mon.restart_events()
    yield
    state["cur"], state["opened"], state["attrs"] = None, None, None
    cls = item.getparent(pytest.Class)
    if cls is not None and cls.nodeid in class_bucket:
        s |= class_bucket[cls.nodeid][0]
        o |= class_bucket[cls.nodeid][1]
        a |= class_bucket[cls.nodeid][2]
    for k in ("__session", "__module", "__package"):
        if k in class_bucket:
            a |= class_bucket[k][2]
    per_test[item.nodeid] = s
    attrs[item.nodeid] = a
    opened[item.nodeid] = o

def pytest_sessionfinish(session):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    files = {}
    def enc(keys):
        d = {}
        for f, l in keys:
            d.setdefault(f, []).append(l)
        return {f: sorted(v) for f, v in d.items()}
    data = {
        "tests": {t: enc(v) for t, v in per_test.items()},
        "opened": {t: sorted(v) for t, v in opened.items()},
        "attrs": {t: sorted(v) for t, v in attrs.items()},
        "import": enc(importtime),
        "mro": mro,
    }
    os.makedirs(OUT, exist_ok=True)
    with _open(os.path.join(OUT, wid + ".json"), "w") as f:
        json.dump(data, f)
