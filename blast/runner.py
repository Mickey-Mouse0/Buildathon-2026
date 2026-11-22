"""Run the selected tests on the patched code and say which ones fail.

A server process imports the pinned sqlglot once, then the test modules a commit
needs. For each commit it forks; the child puts the patch in place and runs the
tests with unittest, as pytest does for these TestCase classes. A test that passes
here on the patched code is dropped by the selector; one that fails, or that the
child never reports on (cut off, crashed), stays selected.

How the patch gets into a child without importing sqlglot again:

- A patched module that isn't imported yet is served from memory when it loads.
- For an imported module, the old and new source are compiled and compared code
  object by code object. The patch qualifies only if the module's own code and
  every class body's code are the same, so all that differs is inside functions
  (lambdas included). Every live function made from a changed code object gets
  the new one. If a function whose own code changes ever started in the server
  (while sqlglot or a test module was importing), something may already have
  been built from its old result, and the server answers "ineligible".

Anything else is "ineligible" too, and the selector keeps the tests as they were.

    python runner.py <sqlglot dir> [<deps dir>]     (pickle frames on stdin/stdout)
"""
import gc
import importlib
import os
import pickle
import signal
import struct
import sys
import time
import types
import unittest

mon = sys.monitoring
TOOL = 3


class InlineExecutor:
    """Stand-in for the tests' ProcessPoolExecutor: the real one spawns workers that
    import sqlglot from disk, without the patch. Arguments and results still make
    the pickle round trip."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        import concurrent.futures as cf

        f = cf.Future()
        try:
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


def code_key(c):
    return (c.co_filename, c.co_firstlineno, c.co_name)


def own_same(a, b):
    """The two code objects do the same thing, nested code aside. Line numbers are
    left out: a comment added above shifts them, and nothing reads them."""
    if (a.co_code != b.co_code or a.co_names != b.co_names or a.co_varnames != b.co_varnames
            or a.co_freevars != b.co_freevars or a.co_cellvars != b.co_cellvars
            or a.co_argcount != b.co_argcount or a.co_kwonlyargcount != b.co_kwonlyargcount
            or a.co_posonlyargcount != b.co_posonlyargcount or a.co_flags != b.co_flags
            or a.co_exceptiontable != b.co_exceptiontable or a.co_name != b.co_name
            or a.co_qualname != b.co_qualname or len(a.co_consts) != len(b.co_consts)):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        xc, yc = isinstance(x, types.CodeType), isinstance(y, types.CodeType)
        if xc != yc:
            return False
        if not xc and (type(x) is not type(y) or repr(x) != repr(y)):
            return False
    return True


def is_function_code(c):
    # module code is "<module>"; a class body's code has no CO_OPTIMIZED flag
    return c.co_name != "<module>" and bool(c.co_flags & 0x1)


def _kids(c):
    return [x for x in c.co_consts if isinstance(x, types.CodeType)]


def _pair_all(old, new, out):
    """Pair up a changed function's code and everything nested in it."""
    ko, kn = _kids(old), _kids(new)
    if len(ko) != len(kn) or old.co_freevars != new.co_freevars or old.co_name != new.co_name \
            or old.co_qualname != new.co_qualname:
        return False
    out.append((old, new, not own_same(old, new)))
    return all(_pair_all(a, b, out) for a, b in zip(ko, kn))


def code_changes(old, new, out):
    """Fill out with (old code, new code, own code differs) for the function code
    objects to replace. False if the patch changes anything outside function code."""
    if not own_same(old, new):
        if not is_function_code(old) or not is_function_code(new):
            return False
        return _pair_all(old, new, out)
    n = len(out)
    for a, b in zip(_kids(old), _kids(new)):
        if not code_changes(a, b, out):
            return False
    if len(out) > n and is_function_code(old):
        out.append((old, new, False))
    return True


def _walk(c):
    yield c
    for x in c.co_consts:
        if isinstance(x, types.CodeType):
            yield from _walk(x)


_started = set()


def _on_start(code, offset):
    _started.add((code.co_filename, code.co_firstlineno, code.co_name))
    return mon.DISABLE


def _send(fd, obj):
    data = pickle.dumps(obj)
    data = struct.pack("<I", len(data)) + data
    while data:
        data = data[os.write(fd, data):]


class _Result(unittest.TestResult):
    def __init__(self, out):
        super().__init__()
        self.out = out
        self.bad = set()

    def _mark(self, test):
        t = getattr(test, "test_case", test)
        self.bad.add(t.id())

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad.add(test.id())

    def stopTest(self, test):
        super().stopTest(test)
        _send(self.out, (test.id(), test.id() in self.bad))


_index = {"modules": -1, "by_key": {}}


def _reindex():
    """Every live function by its code's key, and the heap frozen: a child's garbage
    collector then never walks (and so never copies) what the server built."""
    if _index["modules"] == len(sys.modules):
        return
    gc.unfreeze()
    by_key = {}
    for f in gc.get_objects():
        if type(f) is types.FunctionType:
            c = f.__code__
            by_key.setdefault((c.co_filename, c.co_firstlineno, c.co_name), []).append(f)
    _index["by_key"] = by_key
    _index["modules"] = len(sys.modules)
    gc.collect()
    gc.freeze()


def _swap(changes):
    """Give every live function made from a changed code object its new code."""
    for old, new, _ in changes:
        for f in _index["by_key"].get(code_key(old), ()):
            if f.__code__.co_qualname == old.co_qualname:
                f.__code__ = new


def _child(src, job, plan, cases, wfd):
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if job["overlay"]:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from importstate import _Overlay

            sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
        if plan:
            _swap(plan)
        suite = unittest.TestSuite(cases)
        res = _Result(wfd)
        suite.run(res)
        # a class or module fixture that failed: unittest reports it once, pytest
        # reports every test of the class as an error
        holders = [str(t) for t, _ in res.errors if type(t).__name__ == "_ErrorHolder"]
        _send(wfd, ("#holders", holders))
    finally:
        os._exit(0)


def serve(src, deps):
    src = os.path.abspath(src)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 1000))
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, src)
    if deps and os.path.isdir(deps):
        sys.path.append(deps)
    os.chdir(src)
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    mon.use_tool_id(TOOL, "runner")
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.set_events(TOOL, mon.events.PY_START)
    import sqlglot  # noqa: F401

    t = os.times()
    print(f"runner: up after {t.user + t.system:.2f}s CPU", file=sys.stderr)
    # Replies go out on a private copy of stdout; fd 1 itself now points at stderr,
    # so nothing a test, a child or a C library prints can land in the protocol.
    inp, outp = sys.stdin.buffer, os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    mods = {}
    compiled = {}
    while True:
        head = inp.read(4)
        if len(head) < 4:
            t = os.times()
            print(f"runner: {t.user + t.system:.2f}s CPU here, {t.children_user + t.children_system:.2f}s in children",
                  file=sys.stderr)
            return
        job = pickle.loads(inp.read(struct.unpack("<I", head)[0]))
        t0 = os.times()
        try:
            reply = handle(src, job, mods, compiled)
        except BaseException as e:
            reply = {"status": f"error {e!r}", "results": {}}
        t1 = os.times()
        reply["cpu"] = (t1.user + t1.system - t0.user - t0.system,
                        t1.children_user + t1.children_system - t0.children_user - t0.children_system)
        data = pickle.dumps(reply)
        outp.write(struct.pack("<I", len(data)) + data)
        outp.flush()


def _plan(src, job, compiled):
    """(code objects to swap, None) for the patched modules already imported, or
    (None, why not)."""
    plan = []
    for rel, new_src in job["overlay"].items():
        path = os.path.join(src, rel)
        modname = rel[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[:-9]
        if modname not in sys.modules:
            continue  # loads from the overlay in the child
        if rel not in compiled:
            with open(path, encoding="utf-8") as f:
                compiled[rel] = compile(f.read(), path, "exec", dont_inherit=True)
        try:
            new = compile(new_src, path, "exec", dont_inherit=True)
        except SyntaxError:
            return None, "syntax error"
        changes = []
        if not code_changes(compiled[rel], new, changes):
            return None, f"{rel} changes outside function code"
        if rel not in keycount:
            kc = keycount[rel] = {}
            for c in _walk(compiled[rel]):
                k = code_key(c) + (c.co_qualname,)
                kc[k] = kc.get(k, 0) + 1
        for old, _, own in changes:
            if own and code_key(old) in _started:
                return None, f"{old.co_qualname} already ran in the server"
            if keycount[rel][code_key(old) + (old.co_qualname,)] > 1:
                return None, f"{old.co_qualname} shares its line with a namesake"
        plan.extend(changes)
    return plan, None


keycount = {}
# Classes whose setUpClass is too slow to repeat in every child (TestExecutor loads
# every TPC table, about 2 s). Their DuckDB connections don't survive the fork; only
# the TPC tests use them, and the selector never sends those here.
PRESETUP = {"tests/test_executor.py::TestExecutor"}
_setup_done = set()


_tpc = {}


def handle_tpc(src, job, compiled):
    """The TPC check (blast/tpcmap.py) in a fork: the tables and the pinned commit's
    results are loaded once here instead of once per check."""
    plan, why = _plan(src, job, compiled)
    if plan is None:
        return {"status": f"ineligible: {why}"}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tpcmap

    if not _tpc:
        with open(job["expected"], "rb") as f:
            _tpc["expected"] = pickle.load(f)
        _tpc["tables"] = tpcmap.class_setup(src)
        for test_id in tpcmap.SUITES:
            tpcmap.suite(src, test_id)
        # what loading the tables ran is in _started now
        plan, why = _plan(src, job, compiled)
        if plan is None:
            return {"status": f"ineligible: {why}"}
    _reindex()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            sys.setrecursionlimit(20000)

            def out_of_time(*_):
                print("tpccheck: out of time", file=sys.stderr)
                os._exit(0)

            signal.signal(signal.SIGALRM, out_of_time)
            signal.setitimer(signal.ITIMER_REAL, job.get("front_deadline", 3600))
            if job["overlay"]:
                from importstate import _Overlay

                sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in job["overlay"].items()}))
            if plan:
                _swap(plan)
            _send(w, ("verdict", tpcmap.check_job(src, job, _tpc["expected"], _tpc["tables"])))
        finally:
            os._exit(0)
    os.close(w)
    buf, done = _collect(r, pid, job.get("timeout", 30))
    if not done or len(buf) < 4:
        return {"status": "cut off"}
    return {"status": "ok", "verdict": pickle.loads(buf[4:])[1]}


def _collect(r, pid, timeout):
    """Everything the child writes until it exits or the time is up (then it is killed)."""
    import select

    deadline = time.monotonic() + timeout
    buf = b""
    done = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            break
        chunk = os.read(r, 65536)
        if not chunk:
            done = True
            break
        buf += chunk
    os.close(r)
    if not done:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    os.waitpid(pid, 0)
    return buf, done


def handle(src, job, mods, compiled):
    if job.get("kind") == "tpc":
        return handle_tpc(src, job, compiled)
    # a patch that can't be swapped in is turned away before any test module loads
    plan, why = _plan(src, job, compiled)
    if plan is None:
        return {"status": f"ineligible: {why}", "results": {}}
    # importing a test module may load more of sqlglot
    cases, ids = [], {}
    for tid in job["tests"]:
        file, cls, meth = tid.split("::")
        name = file[:-3].replace("/", ".")
        if name not in mods:
            try:
                mods[name] = importlib.import_module(name)
            except BaseException as e:
                mods[name] = e
        if isinstance(mods[name], BaseException):
            return {"status": f"ineligible: {name} does not import here ({mods[name]!r})", "results": {}}
        klass = getattr(mods[name], cls)
        if tid.rsplit("::", 1)[0] in PRESETUP and klass not in _setup_done:
            # Before the fork, once: what it runs lands in _started, so a patch to any
            # of that code is turned away below.
            _setup_done.add(klass)
            try:
                klass.setUpClass()
            except BaseException as e:
                mods[name] = e
                return {"status": f"ineligible: {cls} setup fails here ({e!r})", "results": {}}
            klass.setUpClass = classmethod(lambda c: None)
            klass.tearDownClass = classmethod(lambda c: None)
        case = klass(meth)
        cases.append(case)
        ids[case.id()] = tid
    # again now that the test modules (and whatever they load) are imported
    plan, why = _plan(src, job, compiled)
    if plan is None:
        return {"status": f"ineligible: {why}", "results": {}}
    _reindex()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(src, job, plan, cases, w)
    os.close(w)
    buf, done = _collect(r, pid, job.get("timeout", 10))
    results, holders = {}, None
    pos = 0
    while pos + 4 <= len(buf):
        n = struct.unpack("<I", buf[pos:pos + 4])[0]
        if pos + 4 + n > len(buf):
            break
        k, v = pickle.loads(buf[pos + 4:pos + 4 + n])
        pos += 4 + n
        if k == "#holders":
            holders = v
        elif k in ids:
            results[ids[k]] = v
    if holders is None:
        # the child did not finish: only what it reported as failing is certain,
        # and the test it was in when it stopped is unknown
        return {"status": "cut off", "results": {k: v for k, v in results.items()}, "partial": True}
    if holders:
        return {"status": f"fixture error {holders[:2]}", "results": {}}
    return {"status": "ok", "results": results}


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
