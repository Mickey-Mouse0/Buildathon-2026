"""Tokenizer replay: drop tests whose tokenizer runs give the same results under a patch.

A patch that only changes code that runs inside the tokenizer (Tokenizer.__init__ and
Tokenizer.tokenize, and everything they call that never runs anywhere else) can only
reach a test through what those calls return and leave behind. Tokenizing resets all
per-call state, so each call is a function of (tokenizer class, dialect, sql). We
recorded every such call each test makes (tools/tokreplay_plugin.py, checking in the
test that a fresh tokenizer gives the same result as the test's own). If every call
of a test gives the same tokens, errors, tokenizer state and log output with the
patched code, the test runs exactly as before.

A changed setting (a class attribute) also counts as inside when the import diff
shows it is the only thing that changed and the tests that read it outside the
tokenizer are kept.

    python tokreplay.py base <sqlglot dir> <tokreplay.pkl.xz> <out.pkl>   (build time)
"""
import ast
import enum
import hashlib
import importlib
import logging
import lzma
import os
import pickle
import sys
import warnings

STATE = ("sql", "size", "tokens", "_start", "_current", "_line", "_col", "_comments", "_char", "_end", "_peek",
         "_prev_token_line")


class Opaque(Exception):
    pass


def _enc(x, memo):
    if x is Opaque:
        return "unset"
    t = type(x)
    if x is None or t in (str, int, float, bool, bytes):
        return repr(x)
    if isinstance(x, enum.Enum):
        return f"E{t.__module__}.{t.__qualname__}.{x._name_}"
    i = memo.get(id(x))
    if i is not None:
        return f"@{i}"
    memo[id(x)] = len(memo)
    if t in (list, tuple):
        return t.__name__[0] + "[" + ",".join(_enc(v, memo) for v in x) + "]"
    if isinstance(x, (set, frozenset)):
        return "S[" + ",".join(sorted(_enc(v, {}) for v in x)) + "]"
    if isinstance(x, dict):
        return "D{" + ",".join(sorted(_enc(k, {}) + ":" + _enc(v, {}) for k, v in x.items())) + "}"
    slots = [s for c in t.__mro__ for s in getattr(c, "__slots__", ()) if isinstance(s, str)]
    if t.__module__.startswith("sqlglot") and slots and not hasattr(x, "__dict__"):
        return f"O{t.__qualname__}(" + ",".join(_enc(getattr(x, s, Opaque), memo) if s != "__weakref__" else ""
                                               for s in slots) + ")"
    raise Opaque(t.__qualname__)


def _err(e):
    if e is None:
        return "-"
    parts = [type(e).__module__, type(e).__qualname__, str(e), repr(sorted((k, repr(v)) for k, v in vars(e).items()))]
    c = e.__cause__ or e.__context__
    if c is not None:
        parts += [type(c).__qualname__, str(c)]
    return repr(parts)


def outcome(tok, tokens, err):
    """Everything a caller can see after one tokenize call, as text."""
    memo = {}
    core = tok._core
    state = [_enc(getattr(core, s, Opaque), memo) for s in STATE]
    return "|".join([_enc(tokens, memo), _err(err), *state])


def digest(text):
    return hashlib.blake2b(text.encode(errors="surrogatepass"), digest_size=12).hexdigest()


def _plain(v):
    """v as builtins only (enums by name), so the data file loads without sqlglot."""
    t = type(v)
    if v is None or t in (str, int, float, bool, bytes):
        return v
    if isinstance(v, enum.Enum):
        return ("__enum__", f"{t.__module__}:{t.__qualname__}", v._name_)
    if t is tuple:
        return ("__tuple__", tuple(_plain(x) for x in v))
    if t is list:
        return ("__list__", tuple(_plain(x) for x in v))
    if t is dict:
        return ("__dict__", tuple((_plain(k), _plain(x)) for k, x in v.items()))
    raise Opaque(t.__qualname__)


def _unplain(v):
    if type(v) is not tuple:
        return v
    tag, body = v[0], v[1:]
    if tag == "__enum__":
        return getattr(_resolve(body[0]), body[1])
    if tag == "__tuple__":
        return tuple(_unplain(x) for x in body[0])
    if tag == "__list__":
        return [_unplain(x) for x in body[0]]
    if tag == "__dict__":
        return {_unplain(k): _unplain(x) for k, x in body[0]}
    raise ValueError(tag)


def describe_dialect(d):
    """(class key, fields) for a Dialect instance as plain data, or None."""
    cls = type(d)
    try:
        fields = tuple(sorted((k, _plain(v)) for k, v in vars(d).items()))
    except Opaque:
        return None
    return f"{cls.__module__}:{cls.__qualname__}", fields


def _resolve(key):
    mod, qual = key.split(":")
    obj = importlib.import_module(mod)
    for part in qual.split("."):
        obj = getattr(obj, part)
    return obj


def build(tok_key, dialect_key, fields):
    dcls = _resolve(dialect_key)
    d = dcls.__new__(dcls)
    for k, v in fields:
        object.__setattr__(d, k, _unplain(v))
    return _resolve(tok_key)(dialect=d)


def run(tok_key, dialect_key, fields, sql):
    """Digest of one fresh tokenizer call, log records and warnings included."""
    logs = []
    orig = logging.Logger._log

    def capture(self, level, msg, args, *a, **kw):
        logs.append((self.name, level, str(msg), repr(args)))

    enabled = logging.Logger.isEnabledFor
    logging.Logger._log = capture
    logging.Logger.isEnabledFor = lambda self, level: True
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tok = build(tok_key, dialect_key, fields)
            err = tokens = None
            try:
                tokens = tok.tokenize(sql)
            except Exception as e:
                err = e
            text = outcome(tok, tokens, err)
    except Opaque:
        return None
    except Exception as e:
        text = "build failed " + _err(e)
        caught = []
    finally:
        logging.Logger._log = orig
        logging.Logger.isEnabledFor = enabled
    return digest(text + "|" + repr(logs) + "|" + repr([(w.category.__qualname__, str(w.message)) for w in caught]))


def replay(inputs, budget=None, base=None, probe=0, probe_max=1.0):
    """Digests for [(tokenizer key, dialect key, dialect fields, sql)], in order. Past
    `budget` CPU seconds the rest are None (not replayed). With base digests, if more
    than probe_max of the first `probe` calls differ, the patch changes tokenizing
    broadly and the rest are skipped too."""
    import time

    stop = None if budget is None else time.process_time() + budget
    out = []
    differ = 0
    for n, (tok_key, dialect_key, fields, sql) in enumerate(inputs):
        if (stop is not None and time.process_time() > stop) or (probe and n == probe and differ > probe_max * probe):
            out.extend([None] * (len(inputs) - n))
            break
        try:
            d = run(tok_key, dialect_key, fields, sql)
        except RecursionError:
            d = None
        out.append(d)
        if base is not None and d != base[n]:
            differ += 1
    return out


def changed_entries(before, after):
    """importstate entries that differ between two snapshots (as the import diff
    counts them), or None if the patched sqlglot no longer imports."""
    if set(after["fail"]) - set(before["fail"]):
        return None
    unstable = before.get("unstable", set())
    ba, aa = before.get("alias", {}), after.get("alias", {})
    bs, as_ = before["snap"], after["snap"]
    out = []
    for k in bs.keys() | as_.keys():
        b, a = bs.get(k), as_.get(k)
        if ba.get(k) != aa.get(k):
            out.append(k)
        elif b != a and k not in unstable and not (b and a and b[0].endswith("!opaque") and a[0].endswith("!opaque")):
            out.append(k)
    return out


def load(path):
    with lzma.open(path, "rb") as f:
        return pickle.load(f)


def _pure_inserts(fc):
    """Old line numbers that added lines go in front of, leaving out lines that only
    replace removed ones (those are covered by the removed lines)."""
    out = set()
    for start, count, lines in fc.hunks:
        o, replacing = start, False
        for ln in lines:
            if ln.startswith("-"):
                o += 1
                replacing = True
            elif ln.startswith("+"):
                if not replacing:
                    out.add(o)
                    if count == 0:
                        out.add(o + 1)  # "@@ -L,0" inserts after line L
            elif ln.startswith(" ") or ln == "":
                o += 1
                replacing = False
    return out


def changed_spots(files, src):
    """For each changed .py file under sqlglot/: the changed lines that sit in a function
    body, and whether any changed line sits elsewhere (class body, module level, a def
    line). None if a file can't be read this way."""
    body, elsewhere = {}, False
    for fc in files:
        p = fc.path
        if not (p.startswith("sqlglot/") and p.endswith(".py")) or fc.old_path is None or fc.new_path is None:
            return None
        try:
            with open(os.path.join(src, fc.old_path)) as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            return None
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
        spots = set(fc.removed_lines())
        for l in _pure_inserts(fc):
            spots |= {l, l - 1}
        for l in spots:
            inner = [n for n in fns if n.lineno <= l <= n.end_lineno]
            if not inner:
                elsewhere = True
                continue
            fn = max(inner, key=lambda n: n.lineno)
            first = fn.body.lineno if isinstance(fn, ast.Lambda) else fn.body[0].lineno
            if l < first or (isinstance(fn, ast.Lambda) and l == fn.lineno and fn.lineno == fn.end_lineno):
                elsewhere = True
                continue
            body.setdefault(p, set()).add(l)
    return body, elsewhere


class TokReplay:
    """Runtime side: what the recording says, and the verdict for one patch."""

    def __init__(self, path, base_path, test_index):
        d = load(path)
        self.inputs = d["inputs"]
        self.outside = d["outside"]
        self.out_reads = d["out_reads"]
        self.wrapped = d["wrapped"]
        self.collect = d["collect"]
        self.collect_calls = d["collect_calls"] if not d.get("collect_bad") else None
        self.idx = [test_index.get(t) for t in d["tests"]]
        self.calls = {}
        for ti, ins in enumerate(d["calls"]):
            i = self.idx[ti]
            if i is not None:
                self.calls[i] = ins
        with open(base_path, "rb") as f:
            self.base = pickle.load(f)

    def inside_only(self, body):
        for path, lines in body.items():
            ranges = self.outside.get(path, ())
            for l in lines:
                if any(a <= l <= b for a, b in ranges):
                    return False
        return True

    def outside_readers(self, attrs):
        """Tests that read any of these settings outside the tokenizer, or None if one
        of them was never watched."""
        out = set()
        for a in attrs:
            if a not in self.wrapped:
                return None
            for ti in self.out_reads.get(a, ()):
                i = self.idx[ti]
                if i is not None:
                    out.add(i)
        return out

    def cost_ms(self, need):
        """Rough CPU of replaying these inputs (about 0.2 ms a call, 2.5 us a char)."""
        return sum(0.2 + len(self.inputs[i][3]) / 400 for i in need)

    def needed(self, tests):
        """Input indexes to replay for these tests; tests without a usable recording."""
        need, unknown = set(), set()
        for t in tests:
            ins = self.calls.get(t)
            if ins is None:
                unknown.add(t)
            else:
                need.update(ins)
        return sorted(need), unknown

    def affected(self, tests, done):
        """Tests with a call whose patched digest differs (done: {input index: digest})."""
        out = set()
        for t in tests:
            for i in self.calls.get(t) or ():
                b, a = self.base[i], done.get(i)
                if b is None or a is None or a != b:
                    out.add(t)
                    break
        return out


def main():
    cmd, src = sys.argv[1], os.path.abspath(sys.argv[2])
    sys.path.insert(0, src)
    if cmd == "base":
        data = load(sys.argv[3])
        digests = replay(data["inputs"])
        with open(sys.argv[4], "wb") as f:
            pickle.dump(digests, f, protocol=pickle.HIGHEST_PROTOCOL)
        bad = sum(d is None for d in digests)
        print(f"tokreplay: {len(digests)} base digests, {bad} not replayable", file=sys.stderr)


if __name__ == "__main__":
    main()
