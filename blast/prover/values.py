import enum

PRIMITIVES = (type(None), bool, int, float, complex, str, bytes)
MAX_RAW_STR = 256
MAX_RAW_CONTAINER = 16


class Missing:
    __slots__ = ()

    def __repr__(self):
        return "MISSING"

    def __reduce__(self):
        return (_missing, ())


def _missing():
    return MISSING


MISSING = Missing()


class Opaque:
    __slots__ = ("typename", "truthy", "ident", "eqmode", "eqkey")

    def __init__(self, typename, truthy, ident, eqmode, eqkey):
        self.typename = typename
        self.truthy = truthy
        self.ident = ident
        self.eqmode = eqmode
        self.eqkey = eqkey

    def _key(self):
        return (self.typename, self.truthy, self.ident, self.eqmode, self.eqkey)

    def __eq__(self, other):
        return isinstance(other, Opaque) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f"Opaque({self.typename}, truthy={self.truthy}, id={self.ident}, eq={self.eqmode})"

    def __reduce__(self):
        return (Opaque, self._key())


def _is_picklable_ref(obj):
    module = getattr(obj, "__module__", "") or ""
    qualname = getattr(obj, "__qualname__", "") or ""
    return (module == "builtins" or module.startswith("sqlglot")) and "<locals>" not in qualname


def _truthiness(value):
    try:
        return bool(value)
    except Exception:
        return None


def _raw_container(value):
    if len(value) > MAX_RAW_CONTAINER:
        return False
    for item in value:
        if type(item) not in PRIMITIVES:
            return False
        if type(item) in (str, bytes) and len(item) > MAX_RAW_STR:
            return False
    return True


def encode(value, identities):
    kind = type(value)
    if kind in PRIMITIVES:
        if kind not in (str, bytes) or len(value) <= MAX_RAW_STR:
            return value
        return Opaque(kind.__name__, bool(value), None, "value", hash(value))
    if kind in (tuple, frozenset) and _raw_container(value):
        return value
    if isinstance(value, enum.Enum) and _is_picklable_ref(kind):
        return value
    if isinstance(value, type) and _is_picklable_ref(value):
        return value
    ident = identities.setdefault(id(value), len(identities))
    default_eq = getattr(kind, "__eq__", None) is object.__eq__ and getattr(kind, "__ne__", None) is object.__ne__
    eqmode = "identity" if default_eq else "custom"
    return Opaque(f"{kind.__module__}.{kind.__qualname__}", _truthiness(value), ident, eqmode, None)
