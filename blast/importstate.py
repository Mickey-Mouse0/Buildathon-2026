"""What sqlglot looks like right after it is imported, as a fingerprint per setting.

Import-time code (metaclasses, __init_subclass__, helpers called from class bodies)
reads some settings to build others, e.g. Dialect's metaclass turns TIME_MAPPING
into TIME_TRIE. Per-test coverage can't see who depends on that, because it all
happens before the first test. Instead of guessing, we import the patched sqlglot
in a child process and compare every class attribute and module global with the
same fingerprint taken from the pinned commit. Whatever differs is exactly what
the patch changed at import time, and the coverage map knows who reads it.

    python importstate.py snapshot <sqlglot dir> <out.pkl>        (build time)
    python importstate.py patched <sqlglot dir>   < {relpath: new source}   (pickles in and out)

Each entry is key -> (digest, kind, own_fn):
    key  = ("a", "module:Class.Qual", name) for a class attribute
           ("g", "module", name)            for a module global
    kind = "plain"    a setting the coverage plugin records reads of
           "callable" a function, method, class or descriptor
           "dunder"   __x__ names
           "enum"     a member of an Enum class (the plugin does not wrap those)
           "module"   a module object
own_fn is True for a function or class defined right there with def/class.
A digest marked opaque (see OPAQUE) means part of the value could not be read, so
two equal digests do not prove the values are equal.
"""
# sqlglot is stricter once unittest or pytest is loaded (expressions/core.py UNITTEST:
# an argument a class doesn't declare raises TypeError). Every real test runs that way,
# so this process must too, before sqlglot is imported.
import unittest  # noqa: F401
import enum
import functools
import hashlib
import importlib
import importlib.abc
import importlib.util
import os
import pickle
import pkgutil
import re
import sys
import types

OPAQUE = "!opaque"


_blake = hashlib.blake2b


def _h(*parts):
    # bytes parts (child digests, often megabytes joined) go in raw, length first,
    # instead of through repr(), which was most of the time spent here
    return _blake(b"\0".join([b"%d:" % len(p) + p if type(p) is bytes else repr(p).encode() for p in parts]),
                  digest_size=12).digest()


_LEAVES = (type(None), bool, int, float, complex, str, bytes)


class _Canon:
    """Digest of a value, built from its parts' digests (memoized per object)."""

    def __init__(self):
        self.memo = {}
        # one table per leaf type: True == 1 and 0.0 == -0.0 must not share a digest
        self.leaves = {t: {} for t in _LEAVES}
        self.opaque = False

    def __call__(self, v):
        t = type(v)
        table = self.leaves.get(t)
        if table is not None:
            k = repr(v) if t is float or t is complex else v
            hit = table.get(k)
            if hit is None:
                hit = table[k] = _h(t.__name__, v)
            return hit
        if isinstance(v, enum.Enum):
            return _h("enum", type(v).__module__, type(v).__qualname__, v.name)
        key = id(v)
        hit = self.memo.get(key)
        if hit is not None and hit[0] is v:
            return hit[1]
        # The entry holds v itself: many values here are temporary (the field dicts
        # built for objects and classes below), and once one is freed its id would
        # be reused by the next, which would then get the freed one's digest.
        self.memo[key] = (v, b"cycle")  # placeholder while we recurse
        out = self._compound(v, t)
        self.memo[key] = (v, out)
        return out

    def _compound(self, v, t):
        if t in (tuple, list):
            items = [self(x) for x in v]
            if items and all(isinstance(x, (type, types.FunctionType)) for x in v):
                # tuple(set of classes): the order follows object addresses, which
                # differ between processes, so it carries no meaning we can compare
                items.sort()
            return _h(t.__name__, b"".join(items))
        if isinstance(v, dict):
            # sorted: several tables are built by iterating sets, so their order
            # depends on the hash seed of the process
            return _h("dict", t.__name__, b"".join(sorted(self(k) + self(x) for k, x in v.items())))
        if isinstance(v, (set, frozenset)):
            return _h(t.__name__, b"".join(sorted(self(x) for x in v)))
        if isinstance(v, type):
            if _named(v):
                return _h("cls", v.__module__, v.__qualname__)
            # Built at import and only reachable through this reference, e.g. the
            # Tokenizer class Dialect's metaclass makes for a dialect without its own.
            # Its settings are listed nowhere else, so they count as part of the value.
            fields = {k: x for k, x in v.__dict__.items() if k not in _SKIP}
            return _h("dyncls", v.__module__, v.__qualname__, self(v.__bases__), self(fields))
        if isinstance(v, types.ModuleType):
            return _h("mod", v.__name__)
        if isinstance(v, types.MethodType):
            owner = self(v.__self__) if isinstance(v.__self__, type) else _h("obj", type(v.__self__).__qualname__)
            return _h("bound", owner, self(v.__func__))
        if isinstance(v, types.FunctionType):
            cells = []
            for c in v.__closure__ or ():
                try:
                    cells.append(self(c.cell_contents))
                except ValueError:
                    cells.append(b"empty")
            return _h("fn", v.__module__, v.__qualname__, b"".join(cells), self(v.__defaults__), self(v.__kwdefaults__))
        if isinstance(v, functools.partial):
            return _h("partial", self(v.func), self(v.args), self(v.keywords))
        if isinstance(v, (staticmethod, classmethod)):
            return _h(t.__name__, self(v.__func__))
        if isinstance(v, property):
            return _h("property", self(v.fget), self(v.fset), self(v.fdel))
        if isinstance(v, functools.cached_property):
            return _h("cached_property", self(v.func))
        if isinstance(v, re.Pattern):
            return _h("re", v.pattern, v.flags)
        if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType)):
            return _h("builtin", getattr(v, "__module__", None), v.__qualname__)
        d = getattr(v, "__dict__", None)
        slots = [s for c in t.__mro__ for s in getattr(c, "__slots__", ()) if isinstance(s, str)]
        if isinstance(d, dict) or slots:
            fields = dict(d) if isinstance(d, dict) else {}
            for s in slots:
                if s not in fields and hasattr(v, s):
                    fields[s] = getattr(v, s)
            return _h("obj", t.__module__, t.__qualname__, self(fields))
        r = repr(v)
        if " at 0x" in r or " object at " in r:
            self.opaque = True
            return _h(OPAQUE, t.__module__, t.__qualname__)
        return _h("repr", t.__module__, t.__qualname__, r)


_SKIP = ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__", "__static_attributes__")


def _named(cls):
    """Is the class reachable as module.Qual.Name, so snapshot() lists it itself?"""
    obj = sys.modules.get(cls.__module__)
    for part in cls.__qualname__.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj is cls


def _digest(canon, v):
    canon.opaque = False
    h = canon(v).hex()
    return h + OPAQUE if canon.opaque else h


def _own_fn(owner_mod, qual, val):
    """True for a function written right there with def (its body and defaults are
    the line rules' business), as opposed to one built or borrowed at import."""
    f = val
    if isinstance(f, (staticmethod, classmethod)):
        f = f.__func__
    elif isinstance(f, property):
        f = f.fget
    elif isinstance(f, functools.cached_property):
        f = f.func
    if isinstance(f, type):
        return f.__module__ == owner_mod and f.__qualname__ == qual
    return isinstance(f, types.FunctionType) and f.__module__ == owner_mod and f.__qualname__ == qual


def _kind(cls, name, val):
    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    if issubclass(cls, enum.Enum):
        return "enum"
    if (callable(val) and not isinstance(val, type)) or hasattr(type(val), "__get__") or isinstance(val, type):
        return "callable"
    return "plain"


def import_all():
    """Import every sqlglot module, as the coverage plugin does. Returns the ones that fail."""
    fail = []
    try:
        import sqlglot
    except Exception:
        return ["sqlglot"]
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.", onerror=lambda name: fail.append(name)):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception:
            fail.append(m.name)
    return sorted(set(fail))


UNKNOWN = ("?",)
KEYSET = ("K",)
ALL = ("A",)


def norm_key(k):
    """A token for a dict key such that keys a dict lookup treats as equal get equal
    tokens (True, 1 and 1.0 all become ("n", 1)). Keys we can't reason about become
    UNKNOWN, which matches every other token."""
    t = type(k)
    if t is str:
        return ("s", k)
    if k is None:
        return ("0",)
    if t is int or t is bool:
        return ("n", int(k))
    if t is float:
        if k != k or k in (float("inf"), float("-inf")):
            return UNKNOWN
        return ("n", int(k)) if k.is_integer() else ("f", repr(k))
    if t is bytes:
        return ("b", k)
    if t is tuple or t is frozenset:
        parts = [norm_key(x) for x in k]
        if UNKNOWN in parts:
            return UNKNOWN
        return ("t", *parts) if t is tuple else ("fs", *sorted(parts))
    if isinstance(k, enum.Enum):
        if isinstance(k, (str, int)):
            return norm_key(k._value_)
        if t.__eq__ is not object.__eq__:
            return UNKNOWN
        return ("e", t.__module__, t.__qualname__, k._name_)
    if isinstance(k, type):
        if type(k).__eq__ is not type.__eq__ or type(k).__hash__ is not type.__hash__:
            return UNKNOWN
        return ("c", k.__module__, k.__qualname__)
    if isinstance(k, str) and t.__eq__ is str.__eq__ and t.__hash__ is str.__hash__:
        return ("s", str.__str__(k))
    if isinstance(k, int) and t.__eq__ is int.__eq__ and t.__hash__ is int.__hash__:
        return ("n", int.__index__(k))
    return UNKNOWN


def keys_match(a, b):
    return a == b or a == UNKNOWN or b == UNKNOWN


def dict_info(canon, d):
    """Per-key fingerprints of a dict: {key digest: (norm_key, value digest)}, plus
    the digest of its key order (what iteration and len() depend on)."""
    keys = {}
    order = []
    for k, v in d.items():
        kd = _digest(canon, k)
        order.append(kd)
        keys[kd] = (norm_key(k), _digest(canon, v))
    return {"keys": keys, "order": _h(*order).hex()}


def dict_attrs():
    """Every plain dict stored as a class attribute: {snapshot key: dict}, the same
    classes snapshot() walks."""
    out = {}
    for modname, mod in sorted(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if isinstance(val, type) and val.__module__ == modname and val.__qualname__ == name:
                stack = [val]
                while stack:
                    cls = stack.pop()
                    qual = f"{modname}:{cls.__qualname__}"
                    for an, av in list(cls.__dict__.items()):
                        if isinstance(av, type) and av.__module__ == modname and av.__qualname__ == f"{cls.__qualname__}.{an}":
                            stack.append(av)
                        if type(av) is dict and _kind(cls, an, av) == "plain" and an not in _SKIP:
                            out[("a", qual, an)] = av
    return out


def aliases(dicts):
    """Snapshot keys of dicts that are the very same object as another one's."""
    by_id = {}
    for k, d in dicts.items():
        by_id.setdefault(id(d), []).append(k)
    return {k: tuple(sorted(g)) for g in by_id.values() if len(g) > 1 for k in g}


def snapshot():
    canon = _Canon()
    out = {}
    for modname, mod in sorted(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            kind = "module" if isinstance(val, types.ModuleType) else "callable" if callable(val) else "plain"
            out[("g", modname, name)] = (_digest(canon, val), kind, _own_fn(modname, name, val))
            if isinstance(val, type) and val.__module__ == modname and val.__qualname__ == name:
                stack = [val]
                while stack:
                    cls = stack.pop()
                    qual = f"{modname}:{cls.__qualname__}"
                    for an, av in list(cls.__dict__.items()):
                        if an in _SKIP:
                            continue
                        if isinstance(av, type) and av.__module__ == modname and av.__qualname__ == f"{cls.__qualname__}.{an}":
                            stack.append(av)
                        out[("a", qual, an)] = (_digest(canon, av), _kind(cls, an, av),
                                                _own_fn(modname, f"{cls.__qualname__}.{an}", av))
    return out


class _Overlay(importlib.abc.MetaPathFinder, importlib.abc.SourceLoader):
    """Serves patched source for some files; everything else imports normally."""

    def __init__(self, files):
        self.files = {os.path.abspath(k): v for k, v in files.items()}
        self.origin = {}

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.origin and os.path.abspath(spec.origin) in self.files:
            self.origin[fullname] = spec.origin
            return importlib.util.spec_from_file_location(fullname, spec.origin, loader=self,
                                                          submodule_search_locations=spec.submodule_search_locations)
        if spec is None:  # a file the patch adds
            for d in path or sys.path:
                f = os.path.abspath(os.path.join(d, fullname.rpartition(".")[2] + ".py"))
                if f in self.files:
                    self.origin[fullname] = f
                    return importlib.util.spec_from_file_location(fullname, f, loader=self)
        return None

    def get_filename(self, fullname):
        return self.origin[fullname]

    def get_data(self, path):
        p = os.path.abspath(path)
        if p in self.files:
            return self.files[p].encode()
        with open(path, "rb") as f:
            return f.read()

    def path_stats(self, path):
        raise OSError  # never use cached bytecode for patched files


def main():
    sys.setrecursionlimit(20000)
    cmd, src = sys.argv[1], os.path.abspath(sys.argv[2])
    sys.path.insert(0, src)
    want = None
    if cmd == "patched":
        payload = pickle.load(sys.stdin.buffer)
        files, want = payload["files"], payload.get("dicts", {})
        sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in files.items()}))
    fail = import_all()
    result = {"snap": snapshot(), "fail": fail}
    # Per-key fingerprints of class-level dicts (all of them at build time; in a
    # patched run only those whose digest differs from the pinned commit's).
    dicts = dict_attrs()
    result["alias"] = aliases(dicts)
    if want is not None:
        dicts = {k: d for k, d in dicts.items() if want.get(k) != result["snap"][k][0]}
    canon = _Canon()
    result["dicts"] = {k: dict_info(canon, d) for k, d in dicts.items()}
    if cmd == "patched" and payload.get("replay") is not None:
        import tokreplay

        result["replay"] = tokreplay.replay(payload["replay"], payload.get("budget"), payload.get("replay_base"),
                                            payload.get("probe", 0), payload.get("probe_max", 1.0))
    if cmd == "snapshot":
        # entries that differ between two processes (loop variables left in class
        # bodies by iterating a set) are noise, not something a patch did
        s = result["snap"]
        result["unstable"] = set()
        for other in filter(None, os.environ.get("IMPORTSTATE_OTHER", "").split(",")):
            with open(other, "rb") as f:
                o = pickle.load(f)["snap"]
            result["unstable"] |= {k for k in s.keys() | o.keys() if s.get(k) != o.get(k)}
        print(f"importstate: {len(result['unstable'])} unstable entries", file=sys.stderr)
        with open(sys.argv[3], "wb") as f:
            pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"importstate: {len(result['snap'])} entries, {len(fail)} import failures", file=sys.stderr)
    else:
        sys.stdout.buffer.write(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))


if __name__ == "__main__":
    main()
