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


def _h(*parts):
    return hashlib.blake2b(repr(parts).encode(), digest_size=12).digest()


class _Canon:
    """Digest of a value, built from its parts' digests (memoized per object)."""

    def __init__(self):
        self.memo = {}
        self.leaves = {}
        self.opaque = False

    def __call__(self, v):
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            k = (t, v)
            hit = self.leaves.get(k)
            if hit is None:
                hit = self.leaves[k] = _h(t.__name__, v)
            return hit
        if isinstance(v, enum.Enum):
            return _h("enum", type(v).__module__, type(v).__qualname__, v.name)
        key = id(v)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        self.memo[key] = b"cycle"  # placeholder while we recurse
        out = self._compound(v, t)
        self.memo[key] = out
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
            return _h("cls", v.__module__, v.__qualname__)
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
                        if an in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__", "__static_attributes__"):
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
    if cmd == "patched":
        files = pickle.load(sys.stdin.buffer)
        sys.meta_path.insert(0, _Overlay({os.path.join(src, k): v for k, v in files.items()}))
    fail = import_all()
    result = {"snap": snapshot(), "fail": fail}
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
