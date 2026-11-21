"""Split the heavy-test profiles by sqlglot file, so a run loads only what its patches touch.

    python shard.py <profiles.pkl[.xz]> <target_index.json.gz> <sqlglot dir> <out>

Run at image build time. It only regroups the recorded data: every record, overflow
and raised entry lands in the shard of the file its expression is in, unchanged.

The output is one file: an 8-byte length, a pickled header, then one pickle per
file. The header is {"tests": {test id: exitstatus}, "index": {path: (offset, size)}}
and each shard is {test id: {"records": {...}, "overflow": [...], "raised": [...]}}.
"""
import gzip
import json
import lzma
import pickle
import sys


def split(profiles, file_of):
    """{path: {test id: that test's profile, restricted to expressions in path}}."""
    shards = {}
    for test_id, profile in profiles.items():
        for field in ("overflow", "raised"):
            for number in profile.get(field, ()):
                part = shards.setdefault(file_of[number], {}).setdefault(test_id, {})
                part.setdefault(field, []).append(number)
        for number, records in profile["records"].items():
            part = shards.setdefault(file_of[number], {}).setdefault(test_id, {})
            part.setdefault("records", {})[number] = records
    return shards


def write(profiles, file_of, out_path):
    blobs = {path: pickle.dumps(part, protocol=pickle.HIGHEST_PROTOCOL) for path, part in split(profiles, file_of).items()}
    index, offset = {}, 0
    for path in sorted(blobs):
        index[path] = (offset, len(blobs[path]))
        offset += len(blobs[path])
    header = pickle.dumps({"tests": {t: p["exitstatus"] for t, p in profiles.items()}, "index": index},
                          protocol=pickle.HIGHEST_PROTOCOL)
    with open(out_path, "wb") as f:
        f.write(len(header).to_bytes(8, "big"))
        f.write(header)
        for path in sorted(blobs):
            f.write(blobs[path])


class ShardedProfiles:
    """Reads the file written above. Shards load on first use."""

    def __init__(self, path):
        self.path = str(path)
        with open(self.path, "rb") as f:
            size = int.from_bytes(f.read(8), "big")
            header = pickle.loads(f.read(size))
        self.base = 8 + size
        self.tests = header["tests"]
        self.index = header["index"]
        self._loaded = {}

    def shard(self, path):
        if path not in self._loaded:
            spot = self.index.get(path)
            if spot is None:
                self._loaded[path] = {}
            else:
                with open(self.path, "rb") as f:
                    f.seek(self.base + spot[0])
                    self._loaded[path] = pickle.loads(f.read(spot[1]))
        return self._loaded[path]

    def items(self):
        return [(test_id, TestProfile(self, test_id)) for test_id in self.tests]


_EMPTY = {"records": {}, "overflow": (), "raised": ()}


class TestProfile:
    """One test's profile, read file by file."""

    def __init__(self, store, test_id):
        self.store = store
        self.test_id = test_id

    def __getitem__(self, key):
        if key != "exitstatus":
            raise KeyError(key)
        return self.store.tests[self.test_id]

    def part(self, path):
        part = self.store.shard(path).get(self.test_id)
        if part is None:
            return _EMPTY
        return {"records": part.get("records", {}), "overflow": part.get("overflow", ()), "raised": part.get("raised", ())}


def main(profiles_path, index_path, sqlglot_dir, out_path):
    sys.path.insert(1, sqlglot_dir)  # the records hold sqlglot enums and classes
    opener = lzma.open if profiles_path.endswith(".xz") else open
    with opener(profiles_path, "rb") as f:
        profiles = pickle.load(f)
    with gzip.open(index_path, "rt") as f:
        file_of = {int(n): key.split(":", 1)[0] for n, key in json.load(f).items()}
    write(profiles, file_of, out_path)
    print(f"shard: {len(profiles)} tests", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:5])
