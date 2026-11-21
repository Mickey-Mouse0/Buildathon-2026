"""Which tests read which keys of each class-level dict (tools/keymap_plugin.py).

A test that read a dict setting but none of the keys a patch changes runs exactly as
before: its first read that could return something new would have to be one of
those keys, KEYSET (iteration, len) when the key order changes, or ALL.
"""
import gzip
import pickle

from importstate import ALL, KEYSET, UNKNOWN


class KeyReads:
    def __init__(self, path, test_index):
        with gzip.open(path, "rb") as f:
            d = pickle.load(f)
        self.gid = {}
        for gid, group in enumerate(d["groups"]):
            for _, qual, name in group:
                self.gid[f"{qual}.{name}"] = gid
        self.idx = [test_index.get(t) for t in d["tests"]]
        self.recorded = frozenset(i for i in self.idx if i is not None)
        self.reads = d["reads"]
        self.collect = d["collect"]

    def covers(self, attr):
        return attr in self.gid

    def readers(self, attr, changed, keyset_changed):
        """Map indexes of tests that read a changed key of the dict setting `attr`
        (normalized keys from importstate.norm_key), or None when reads made outside
        any test (test-module import) could depend on the change."""
        gid = self.gid.get(attr)
        if gid is None:
            return None
        reads = self.reads[gid]
        hit = {ALL, UNKNOWN} | set(changed)
        if keyset_changed:
            hit.add(KEYSET)
        if UNKNOWN in changed:
            hit |= {k for k in reads if k != KEYSET}
        if self.collect[gid] & hit or (UNKNOWN in self.collect[gid] and changed) or (UNKNOWN in changed and self.collect[gid] - {KEYSET}):
            return None
        out = set()
        for k in hit:
            for ti in reads.get(k, ()):
                i = self.idx[ti]
                if i is not None:
                    out.add(i)
        return out


def dict_change(before, after):
    """(normalized keys whose value differs or that exist on one side only, whether
    the key order changed) for two importstate.dict_info records."""
    bk, ak = before["keys"], after["keys"]
    changed = set()
    for kd in bk.keys() | ak.keys():
        b, a = bk.get(kd), ak.get(kd)
        if b is None or a is None or b[1] != a[1] or b[1].endswith("!opaque"):
            changed.add((b or a)[0])
    return changed, before["order"] != after["order"]
