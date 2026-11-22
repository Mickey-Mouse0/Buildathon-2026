"""Runtime check: run the cheap selected tests on the patched code, for real.

A pre-warmed "zygote" process imports every sqlglot module and every test module
once. For each eligible commit it forks; the child swaps the patched code objects
into the live functions (fn.__code__ = new) and runs the selected light tests with
unittest. Tests that pass are dropped, tests that fail stay. Per check this costs
about the tests' own CPU, not the ~1 s of interpreter start and imports.

Only edits confined to function bodies qualify (plan): any change to a module or
class body, to a function that ran while the zygote warmed up (called.pkl, made at
image build), to a closure's free variables, or to the shape of the code tree
means the commit is not checked. Fixture-driven optimizer tests run only the
fixture entries that reach changed code (slice map, made at image build).

    python blast/runcheck.py record <sqlglot dir> <deps dir> <out dir>   (image build)
    python blast/runcheck.py serve <sqlglot dir> <deps dir> <data dir>   (the selector starts it)
"""
import gc
import importlib
import json
import os
import pickle
import pkgutil
import select
import signal
import struct
import sys
import time
import types
import unittest
from concurrent.futures import Future

HERE = os.path.dirname(os.path.abspath(__file__))
# A test that recurses deeper than this fails here, so a test pytest's deeper stack
# would break stays selected; pytest's own frames are far fewer than the margin.
RECURSION_LIMIT = 900
SLICED = ("tests.test_optimizer",)
POOLED = ("tests.test_optimizer", "tests.test_executor")


# ---------------------------------------------------------------- warm-up


def warm(src, deps, record=False):
    os.environ["SKIP_INTEGRATION"] = "0"
    # numpy's BLAS starts worker threads at import; a process with threads must not fork
    for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    sys.path.insert(0, src)
    if deps and deps not in sys.path:
        sys.path.append(deps)
    os.chdir(src)
    called = set()
    if record:
        pre = os.path.join(src, "sqlglot") + os.sep

        def prof(frame, event, arg):
            if event == "call":
                c = frame.f_code
                if c.co_filename.startswith(pre):
                    called.add(key(c))

        sys.setprofile(prof)
    import sqlglot

    failed = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except BaseException as e:
            failed.append((m.name, repr(e)[:80]))
    for root, dirs, fs in os.walk(os.path.join(src, "tests")):
        dirs.sort()
        for f in sorted(fs):
            if f.startswith("test_") and f.endswith(".py"):
                mod = os.path.relpath(os.path.join(root, f), src)[:-3].replace(os.sep, ".")
                try:
                    importlib.import_module(mod)
                except BaseException as e:
                    failed.append((mod, repr(e)[:80]))
    isolate()
    install_slicing()
    sys.setprofile(None)
    return called, failed


def key(c):
    return (c.co_filename, c.co_qualname, c.co_firstlineno)


def index_functions():
    idx = {}
    for ob in gc.get_objects():
        if isinstance(ob, types.FunctionType):
            idx.setdefault(key(ob.__code__), []).append(ob)
    return idx


# ---------------------------------------------------------------- plan


def _same(a, b):
    """Same code apart from nested code objects and line numbers."""
    if (a.co_code != b.co_code or a.co_names != b.co_names or a.co_varnames != b.co_varnames
            or a.co_freevars != b.co_freevars or a.co_cellvars != b.co_cellvars or a.co_flags != b.co_flags
            or a.co_argcount != b.co_argcount or a.co_kwonlyargcount != b.co_kwonlyargcount
            or a.co_posonlyargcount != b.co_posonlyargcount or a.co_exceptiontable != b.co_exceptiontable
            or a.co_name != b.co_name or a.co_qualname != b.co_qualname):
        return False
    ca = [x for x in a.co_consts if not isinstance(x, types.CodeType)]
    cb = [x for x in b.co_consts if not isinstance(x, types.CodeType)]
    return len(ca) == len(cb) and all(type(x) is type(y) and x == y and repr(x) == repr(y) for x, y in zip(ca, cb))


def plan(path, old_src, new_src):
    """([(old key, new code, directly changed)], why). None when the edit reaches
    beyond function bodies."""
    try:
        o = compile(old_src, path, "exec", dont_inherit=True)
        n = compile(new_src, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        return None, f"does not compile ({type(e).__name__})"
    pairs = []

    def walk(a, b, body):
        subs_a = [x for x in a.co_consts if isinstance(x, types.CodeType)]
        subs_b = [x for x in b.co_consts if isinstance(x, types.CodeType)]
        if len(subs_a) != len(subs_b) or a.co_qualname != b.co_qualname or a.co_freevars != b.co_freevars:
            raise ValueError(f"code shape changed at {a.co_qualname}")
        direct = not _same(a, b)
        sub = False
        for x, y in zip(subs_a, subs_b):
            # CO_NEWLOCALS marks a function (or lambda, genexpr); without it, a class body
            sub |= walk(x, y, not (x.co_flags & 0x2))
        changed = direct or sub or a.co_firstlineno != b.co_firstlineno or list(a.co_lines()) != list(b.co_lines())
        if changed:
            if body and direct:
                raise ValueError(f"module or class body changed at {a.co_qualname}")
            if not body:
                pairs.append((key(a), b, direct))
        return changed

    try:
        walk(o, n, True)
    except ValueError as e:
        return None, str(e)
    return pairs, "ok"


# ---------------------------------------------------------------- test running


class _Slow(BaseException):
    pass


def _on_slow(signum, frame):
    raise _Slow("over the per-test CPU cap")


class Result(unittest.TestResult):
    def __init__(self, caps):
        super().__init__()
        self.bad = set()
        self.caps = caps
        self.cpu = {}

    def startTest(self, test):
        tid = getattr(test, "_bl_id", None)
        STATE["calls"] = {}
        STATE["filter"] = STATE["filters"].get(tid)
        signal.setitimer(signal.ITIMER_PROF, self.caps.get(tid, 1.0))
        self._t = time.process_time()
        super().startTest(test)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_PROF, 0)
        self.cpu[getattr(test, "_bl_id", "?")] = round(time.process_time() - self._t, 3)
        super().stopTest(test)

    def _mark(self, t):
        self.bad.add(getattr(t, "_bl_id", "*"))  # a class or module setup error: keep all

    def addError(self, t, err):
        super().addError(t, err)
        self._mark(t)

    def addFailure(self, t, err):
        super().addFailure(t, err)
        self._mark(t)

    def addSubTest(self, t, st, err):
        if err is not None:
            self._mark(t)

    def addUnexpectedSuccess(self, t):
        self._mark(t)


def run_tests(ids, caps):
    suite = unittest.TestSuite()
    r = Result(caps)
    for tid in ids:
        try:
            f, cls, meth = tid.split("::")
            mod = importlib.import_module(f[:-3].replace("/", "."))
            t = getattr(mod, cls)(meth)
        except BaseException:
            r.bad.add(tid)  # can't be loaded here: kept
            continue
        t._bl_id = tid
        suite.addTest(t)
    signal.signal(signal.SIGPROF, _on_slow)
    suite.run(r)
    signal.setitimer(signal.ITIMER_PROF, 0)
    run_tests.cpu = r.cpu
    return r.bad


class InlineExecutor:
    """Stands in for the tests' spawn process pools: the work runs here, on the
    patched code (a spawn worker would import the old code from disk), with the
    pickle round trip a worker does."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        self._init = (initializer, initargs)
        self._inited = False

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            if not self._inited and self._init[0] is not None:
                ini, ia = pickle.loads(pickle.dumps(self._init))
                ini(*ia)
            self._inited = True
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except _Slow:
            raise
        except BaseException as e:
            f.set_exception(e)
        f._bl_entry = STATE["cur"]
        return f

    def map(self, fn, *its, **k):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NoSubprocess(Exception):
    pass


def isolate():
    import subprocess

    def deny(*a, **k):
        raise NoSubprocess("the runtime check runs no subprocesses")

    subprocess.run = subprocess.Popen = subprocess.call = subprocess.check_output = subprocess.check_call = deny
    os.system = deny
    for m in POOLED:
        mod = sys.modules.get(m)
        if mod is not None and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor


# ---------------------------------------------------------------- entry slicing

STATE = {"cur": None, "calls": None, "filter": None, "filters": {}, "rec": None}


def _wrap_loader(orig):
    def gen(filename, *a, **k):
        calls = STATE["calls"]
        if calls is None:
            yield from orig(filename, *a, **k)
            return
        c = calls.get(filename, 0)
        calls[filename] = c + 1
        flt = STATE["filter"]
        for i, item in enumerate(orig(filename, *a, **k)):
            ek = (filename, c, i)
            if flt is not None and ek not in flt:
                continue
            _switch(ek)
            yield item

    return gen


def _switch(ek):
    STATE["cur"] = ek
    rec = STATE["rec"]
    if rec is not None:
        now = time.process_time()
        rec["cpu"][rec["at"]] = rec["cpu"].get(rec["at"], 0.0) + now - rec["t"]
        rec["at"], rec["t"] = ek, now
        rec["sets"].setdefault(ek, set())
        sys.monitoring.restart_events()


def install_slicing():
    for m in SLICED:
        mod = sys.modules.get(m)
        if mod is None:
            continue
        for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            if hasattr(mod, name):
                setattr(mod, name, _wrap_loader(getattr(mod, name)))
    orig_result = Future.result

    def result(self, *a, **k):
        tag = getattr(self, "_bl_entry", None)
        if tag is not None:
            _switch(tag)
        return orig_result(self, *a, **k)

    Future.result = result


TOOL = 4


def record_slices(test_ids, src):
    """Per test: ({entry: set of code keys}, {entry: cpu}, failing). Entry None is
    the work outside any fixture entry."""
    pre = os.path.join(src, "sqlglot") + os.sep
    out = {}

    def cb(code, off):
        if code.co_filename.startswith(pre):
            STATE["rec"]["sets"].setdefault(STATE["rec"]["at"], set()).add(key(code))
        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(TOOL, "blast-slice")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, cb)
    for tid in test_ids:
        STATE.update(cur=None, rec={"sets": {None: set()}, "cpu": {}, "at": None, "t": time.process_time()},
                     filters={})
        sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
        sys.monitoring.restart_events()
        bad = run_tests([tid], {tid: 600.0})
        sys.monitoring.set_events(TOOL, 0)
        rec = STATE["rec"]
        rec["cpu"][rec["at"]] = rec["cpu"].get(rec["at"], 0.0) + time.process_time() - rec["t"]
        out[tid] = (rec["sets"], rec["cpu"], sorted(bad))
        STATE.update(rec=None, cur=None)
    sys.monitoring.free_tool_id(TOOL)
    return out


# ---------------------------------------------------------------- build-time recording


def record(src, deps, out_dir):
    t0 = time.process_time()
    called, failed = warm(src, deps, record=True)
    print(f"runcheck: warm-up {time.process_time() - t0:.2f}s, {len(called)} code objects called, "
          f"{len(failed)} imports failed", file=sys.stderr)
    with open(os.path.join(out_dir, "runcheck_called.pkl"), "wb") as f:
        pickle.dump(called, f, protocol=pickle.HIGHEST_PROTOCOL)
    sys.setrecursionlimit(RECURSION_LIMIT)
    ids = []
    for m in SLICED:
        mod = sys.modules.get(m)
        for name, cls in sorted(vars(mod).items()):
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == m:
                ids += [f"{m.replace('.', '/')}.py::{name}::{t}" for t in unittest.TestLoader().getTestCaseNames(cls)]
    # what each class's setUpClass costs (TestExecutor's loads every TPC table)
    setup = {}
    for name, mod in sorted(sys.modules.items()):
        if not name.startswith("tests.") or mod is None:
            continue
        for cname, cls in sorted(vars(mod).items()):
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == name \
                    and "setUpClass" in vars(cls):
                pid = os.fork()
                if pid == 0:
                    try:
                        signal.alarm(120)
                        cls.setUpClass()
                    finally:
                        os._exit(0)
                ru = os.wait4(pid, 0)[2]
                setup[f"{name.replace('.', '/')}.py::{cname}"] = round(ru.ru_utime + ru.ru_stime, 3)
    print("runcheck: class setups", setup, file=sys.stderr)
    t0 = time.process_time()
    rec = record_slices(ids, src)
    keys, comp = {}, {}
    for tid, (sets, cpu, bad) in rec.items():
        if bad:
            print("runcheck: fails at the pinned commit, never checked:", tid, file=sys.stderr)
            continue
        comp[tid] = ({e: frozenset(keys.setdefault(k, len(keys)) for k in s) for e, s in sets.items()},
                     {e: round(c, 4) for e, c in cpu.items()})
    with open(os.path.join(out_dir, "runcheck_slices.pkl"), "wb") as f:
        pickle.dump((keys, comp, setup), f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"runcheck: sliced {len(comp)} tests, {sum(len(v[0]) for v in comp.values())} entries "
          f"in {time.process_time() - t0:.1f}s", file=sys.stderr)


# ---------------------------------------------------------------- zygote


def _send(fd, obj):
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    data = struct.pack("<I", len(data)) + data
    while data:
        data = data[os.write(fd, data):]


def _read(fd, n, deadline):
    buf = b""
    while len(buf) < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise TimeoutError
        ch = os.read(fd, min(n - len(buf), 1 << 20))
        if not ch:
            raise EOFError
        buf += ch
    return buf


def _recv(fd, deadline=None):
    size = struct.unpack("<I", _read(fd, 4, deadline))[0]
    return pickle.loads(_read(fd, size, deadline))


def serve(src, deps, data_dir):
    t0 = time.process_time()
    warm(src, deps)
    idx = index_functions()
    called = set()
    keys, slices, setup = {}, {}, {}
    try:
        with open(os.path.join(data_dir, "runcheck_called.pkl"), "rb") as f:
            called = pickle.load(f)
        with open(os.path.join(data_dir, "runcheck_slices.pkl"), "rb") as f:
            keys, slices, setup = pickle.load(f)
    except OSError:
        called = None
    try:  # forking a process with threads can deadlock the child
        if len(os.listdir("/proc/self/task")) > 1:
            print("runcheck: zygote has threads after warm-up, not forking", file=sys.stderr)
            called = None
    except OSError:
        pass
    rfd, wfd = sys.stdin.fileno(), sys.stdout.fileno()
    sys.stdout = sys.stderr
    gc.freeze()
    _send(wfd, {"ready": called is not None, "warm": time.process_time() - t0})
    while True:
        try:
            req = _recv(rfd)
        except EOFError:
            return
        try:
            resp = handle(req, src, idx, called, keys, slices, setup)
        except Exception as e:
            resp = {"skip": f"zygote error {e!r}"[:200]}
        _send(wfd, resp)


def handle(req, src, idx, called, keys, slices, setup):
    pairs = []
    for rel, (old, new) in req["files"].items():
        p, why = plan(os.path.join(src, rel), old, new)
        if p is None:
            return {"skip": why}
        pairs += p
    if not pairs:
        return {"skip": "no code changed"}
    direct = [k for k, c, d in pairs if d]
    hot = [k for k in direct if k in called]
    if hot:
        return {"skip": f"changed code runs at import: {hot[0][1]}"}
    changed = {keys[k] for k in direct if k in keys}
    filters, pred, caps = {}, 0.0, {}
    for tid, sec in req["tests"]:
        if tid in slices:
            sets, cpu = slices[tid]
            flt = {e for e, ks in sets.items() if e is not None and ks & changed}
            filters[tid] = flt
            p = 1.3 * (cpu.get(None, 0.0) + sum(cpu.get(e, 0.0) for e in flt)) + 0.01
        else:
            p = 1.4 * sec + 0.01
        pred += p
        caps[tid] = max(1.0, 4 * p)
    # class setups run outside the per-test caps; the child's overall deadline bounds them
    pred += sum(1.3 * setup.get(c, 0.0) for c in {t.rsplit("::", 1)[0] for t, _ in req["tests"]})
    if pred > req["budget"]:
        return {"skip": f"predicted {pred:.2f}s CPU over the {req['budget']:.2f}s budget", "pred": pred}
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            for k, c, d in pairs:
                for fn in idx.get(k, ()):
                    fn.__code__ = c
            sys.setrecursionlimit(RECURSION_LIMIT)
            STATE["filters"] = filters
            bad = run_tests([t for t, _ in req["tests"]], caps)
            _send(w, (sorted(bad), run_tests.cpu))
        except BaseException as e:
            import traceback

            _send(w, {"error": traceback.format_exc()[-600:]})
        finally:
            os._exit(0)
    os.close(w)
    deadline = time.monotonic() + req.get("wall", 2 * pred + 3)
    try:
        bad = _recv(r, deadline)
        if isinstance(bad, dict):
            raise RuntimeError(bad["error"])
        bad, tcpu = bad
        resp = {"bad": bad, "tcpu": tcpu, "pred": pred, "sliced": {t: len(f) for t, f in filters.items()}}
    except (TimeoutError, EOFError, Exception) as e:
        os.kill(pid, signal.SIGKILL)
        resp = {"skip": f"child gave no answer ({type(e).__name__} {str(e)[-300:]!r})", "pred": pred}
    os.close(r)
    _, status, ru = os.wait4(pid, 0)
    resp["cpu"] = ru.ru_utime + ru.ru_stime
    return resp


class Zygote:
    """The selector's handle on the zygote process; started at the first eligible commit."""

    def __init__(self, src, deps, data_dir, timeout):
        import subprocess

        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
        self.p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", src, deps or "", data_dir],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.ok = False
        try:
            hello = _recv(self.p.stdout.fileno(), time.monotonic() + timeout)
            self.ok = bool(hello.get("ready"))
            self.warm = hello.get("warm")
        except Exception:
            self.close()

    def ask(self, req, timeout):
        try:
            _send(self.p.stdin.fileno(), req)
            return _recv(self.p.stdout.fileno(), time.monotonic() + timeout)
        except Exception as e:
            self.close()
            return {"skip": f"zygote lost ({type(e).__name__})"}

    def finish(self):
        """Let it exit (so its CPU is counted like any child's)."""
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.close()

    def close(self):
        self.ok = False
        try:
            self.p.kill()
            self.p.wait(5)
        except Exception:
            pass


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "record":
        record(os.path.abspath(sys.argv[2]), sys.argv[3], os.path.abspath(sys.argv[4]))
    elif cmd == "serve":
        serve(os.path.abspath(sys.argv[2]), sys.argv[3], os.path.abspath(sys.argv[4]))
