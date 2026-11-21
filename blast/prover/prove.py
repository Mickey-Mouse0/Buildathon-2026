import argparse
import ast
import gzip
import json
import os
import pickle
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay import prove_expression  # noqa: E402
from targets import OPERATOR_NODES, find_targets, operator_children, target_key  # noqa: E402

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DOCSTRING_SENSITIVE = ("sqlglot/typing/",)
CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


class Unprovable(Exception):
    pass


def split_patch(text):
    files = []
    current = None
    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            current = {"old": None, "new": None, "hunks": []}
            files.append(current)
        elif current is None:
            continue
        elif raw.startswith("--- ") and not current["hunks"]:
            current["old"] = raw[4:].strip()
        elif raw.startswith("+++ ") and not current["hunks"]:
            current["new"] = raw[4:].strip()
        elif HUNK.match(raw):
            m = HUNK.match(raw)
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            current["hunks"].append({"start": int(m.group(1)), "old_count": old_count, "lines": []})
        elif current["hunks"] and raw[:1] in (" ", "-", "+"):
            current["hunks"][-1]["lines"].append(raw)
        elif current["hunks"] and raw.startswith("\\"):
            continue
    return files


def apply_hunks(old_text, hunks):
    old_lines = old_text.splitlines(keepends=True)
    out = []
    cursor = 0
    for hunk in hunks:
        start = hunk["start"] if hunk.get("old_count") == 0 else hunk["start"] - 1
        if start < cursor:
            raise Unprovable("overlapping hunks")
        out.extend(old_lines[cursor:start])
        cursor = start
        for line in hunk["lines"]:
            tag, body = line[0], line[1:]
            if tag in (" ", "-"):
                if cursor >= len(old_lines) or old_lines[cursor].rstrip("\r\n") != body:
                    raise Unprovable("patch does not apply to the pinned source")
                if tag == " ":
                    out.append(old_lines[cursor])
                cursor += 1
            else:
                out.append(body + "\n")
    out.extend(old_lines[cursor:])
    return "".join(out)


def ast_equal(a, b):
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


def find_diffs(old, new):
    diffs = []

    def walk(a, b, chain):
        if type(a) is not type(b):
            owner = a if isinstance(a, ast.expr) or isinstance(a, ast.stmt) else chain[-1]
            diffs.append((owner, chain if owner is chain[-1] else chain + [a]))
            return
        if isinstance(a, ast.AST):
            here = chain + [a] if isinstance(a, (ast.expr, ast.stmt, ast.mod, ast.comprehension, ast.arguments, ast.excepthandler, ast.keyword, ast.arg)) else chain
            for field in a._fields:
                walk(getattr(a, field, None), getattr(b, field, None), here)
        elif isinstance(a, list):
            if len(a) != len(b):
                diffs.append((chain[-1], chain))
                return
            for x, y in zip(a, b):
                walk(x, y, chain)
        elif a != b or repr(a) != repr(b):
            diffs.append((chain[-1], chain))

    walk(old, new, [])
    return diffs


def counterpart(old_root, old_tree, new_tree):
    path = []

    def locate(node, target):
        if node is target:
            return True
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for i, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        path.append((field, i))
                        if locate(child, target):
                            return True
                        path.pop()
            elif isinstance(value, ast.AST):
                path.append((field, None))
                if locate(value, target):
                    return True
                path.pop()
        return False

    if not locate(old_tree, old_root):
        raise Unprovable("cannot locate expression")
    node = new_tree
    for field, index in path:
        node = getattr(node, field)
        if index is not None:
            node = node[index]
    return node


def is_docstring_change(owner, chain):
    if not isinstance(owner, ast.Constant) or not isinstance(owner.value, str) or len(chain) < 3:
        return False
    expr_stmt, container = chain[-2], chain[-3]
    body = getattr(container, "body", None)
    return isinstance(expr_stmt, ast.Expr) and bool(body) and body[0] is expr_stmt and isinstance(
        container, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    )


def coding_cookie(text):
    return [line for line in text.splitlines()[:2] if CODING_COOKIE.match(line)]


class FileChange:
    def __init__(self, path, old_text, new_text):
        if coding_cookie(old_text) != coding_cookie(new_text):
            raise Unprovable(f"{path}: source encoding declaration changed")
        self.path = path
        self.old_tree = ast.parse(old_text)
        try:
            self.new_tree = ast.parse(new_text)
        except SyntaxError as error:
            raise Unprovable(f"{path}: patched file does not parse ({error.msg})")
        self.roots, self.conditions = find_targets(self.old_tree)
        self.root_ids = {id(r) for r in self.roots}


class Prover:
    def __init__(self, sqlglot_dir, tests, coverage_map, profiles, target_index):
        self.sqlglot_dir = Path(sqlglot_dir)
        if str(self.sqlglot_dir) not in sys.path:
            sys.path.insert(1, str(self.sqlglot_dir))
        self.tests = tests
        self.lines = coverage_map["lines"]
        self.import_lines = {f: set(v) for f, v in coverage_map["import_lines"].items()}
        self.fixtures = coverage_map["fixtures"]
        self.number_of_key = {key: int(number) for number, key in target_index.items()}
        self.profiles = {n: p for n, p in profiles.items() if p["exitstatus"] == 0 and n in tests}
        self.heavy = sorted(self.profiles)
        self.union = None
        self.union_profiled = set()
        self._union_cache = {}

    def attach_union(self, payload, number_of_id):
        self.union = payload
        self.union_profiled = {number_of_id[i] for i in payload["profiled"] if i in number_of_id}

    def covers(self, test, path, start, end):
        lines = self.lines.get(path, {})
        return any(test in lines.get(str(n), ()) for n in range(start, end + 1))

    def imported_by_other_tests(self, path):
        module = path.removesuffix(".py").replace("/", ".")
        stem = Path(path).stem
        pattern = re.compile(rf"(\b{re.escape(module)}\b|from\s+\.+\s*import\s+.*\b{re.escape(stem)}\b|from\s+\.+{re.escape(stem)}\b|\b{re.escape(stem)}\b\s+import)")
        for other in (self.sqlglot_dir / "tests").rglob("*.py"):
            if other.relative_to(self.sqlglot_dir).as_posix() == path:
                continue
            try:
                if pattern.search(other.read_text(errors="replace")):
                    return True
            except OSError:
                return True
        return False

    def analyze(self, patch_text):
        changes, notes = [], []
        affected_by_files = set()
        entries = split_patch(patch_text)
        if not entries:
            raise Unprovable("no file diffs found in the patch")
        for entry in entries:
            old, new = entry["old"], entry["new"]
            if not entry["hunks"]:
                raise Unprovable("file diff without hunks")
            if old == "/dev/null" or new == "/dev/null" or old is None or new is None:
                raise Unprovable("file added or removed")
            path = new.removeprefix("b/")
            if old.removeprefix("a/") != path:
                raise Unprovable("file renamed")
            if path.startswith("tests/") and not path.endswith(".py"):
                readers = set(self.fixtures.get(path, ()))
                affected_by_files |= readers & set(self.heavy)
                notes.append(f"{path}: fixture read by {len(readers)} tests")
                continue
            if path.startswith("tests/"):
                if Path(path).name.startswith("test_") and not self.imported_by_other_tests(path):
                    affected_by_files |= {t for t in self.heavy if self.tests[t]["file"] == path}
                    affected_by_files |= set(self.fixtures.get(path, ())) & set(self.heavy)
                else:
                    affected_by_files |= set(self.heavy)
                notes.append(f"{path}: test code")
                continue
            if not (path.startswith("sqlglot/") and path.endswith(".py")):
                raise Unprovable(f"{path}: not covered by any lemma")
            old_text = (self.sqlglot_dir / path).read_text()
            changes.append(FileChange(path, old_text, apply_hunks(old_text, entry["hunks"])))
        return changes, affected_by_files, notes

    def verdict(self, patch_text):
        try:
            changes, affected, notes = self.analyze(patch_text)
        except Unprovable as error:
            return {"noop": False, "safe": {}, "notes": [f"unprovable: {error}"]}

        obligations = []
        noop = not affected and not any(n.startswith("tests/") for n in notes)
        for change in changes:
            if ast_equal(change.old_tree, change.new_tree):
                notes.append(f"{change.path}: AST unchanged (comments/whitespace only)")
                continue
            diffs = find_diffs(change.old_tree, change.new_tree)
            if not diffs:
                return {"noop": False, "safe": {}, "notes": notes + [f"unprovable: {change.path}: trees differ but no difference located"]}
            if change.path.startswith(DOCSTRING_SENSITIVE) or not all(is_docstring_change(o, c) for o, c in diffs):
                noop = False
                obligations.extend((change, owner, chain) for owner, chain in diffs)
            else:
                notes.append(f"{change.path}: docstring-only change")

        if noop and not obligations:
            return {"noop": True, "safe": {t: "commit provably changes nothing" for t in self.heavy}, "notes": notes}

        safe = {}
        for test in self.heavy:
            if test in affected:
                continue
            reasons = []
            for change, owner, chain in obligations:
                ok, why = self.discharge(test, change, owner, chain)
                if not ok:
                    break
                reasons.append(why)
            else:
                safe[test] = "; ".join(sorted(set(reasons))) or "no obligations"
        return {"noop": False, "safe": safe, "notes": notes}

    @staticmethod
    def replay_root(change, chain):
        for i in range(len(chain) - 1, -1, -1):
            node = chain[i]
            if id(node) in change.root_ids:
                parent = node
                for child in chain[i + 1 :]:
                    if not (isinstance(parent, OPERATOR_NODES) and any(child is c for c in operator_children(parent))):
                        return None
                    parent = child
                return node
        return None

    def union_verdict(self, patch_text):
        if self.union is None:
            return False, "no all-test profiles"
        try:
            changes, affected, notes = self.analyze(patch_text)
        except Unprovable as error:
            return False, f"unprovable: {error}"
        if affected or any(n.startswith("tests/") for n in notes):
            return False, "touches test files or fixtures"
        reasons = []
        for change in changes:
            if ast_equal(change.old_tree, change.new_tree):
                continue
            diffs = find_diffs(change.old_tree, change.new_tree)
            if not diffs:
                return False, "trees differ but no difference located"
            if not change.path.startswith(DOCSTRING_SENSITIVE) and all(is_docstring_change(o, c) for o, c in diffs):
                continue
            data = self.union_file(change.path)
            for owner, chain in diffs:
                root = self.replay_root(change, chain)
                if root is None:
                    return False, "change is not inside a replayable expression"
                number = self.number_of_key.get(target_key(change.path, root))
                if number is None:
                    return False, "expression was not instrumented"
                if number in data["flagged"]:
                    return False, "expression raised, overflowed or ran off the main thread in some test"
                records = data["records"].get(number)
                if records is None:
                    reasons.append("never evaluated by any test")
                    continue
                new_root = counterpart(root, change.old_tree, change.new_tree)
                ok, why = prove_expression(root, new_root, id(root) in change.conditions, records)
                if not ok:
                    return False, why
                reasons.append(why)
        return True, "; ".join(reasons) or "no effective change"

    def union_file(self, path):
        if path not in self._union_cache:
            raw = self.union["files"].get(path)
            self._union_cache[path] = pickle.loads(raw) if raw is not None else {"records": {}, "flagged": []}
        return self._union_cache[path]

    def discharge(self, test, change, owner, chain):
        profile = self.profiles[test]
        root = self.replay_root(change, chain)

        statement = next((n for n in reversed(chain) if isinstance(n, ast.stmt)), None)

        if root is not None:
            number = self.number_of_key.get(target_key(change.path, root))
            if number is None:
                return False, "expression was not instrumented"
            if number in profile["overflow"]:
                return False, "too many distinct evaluations recorded"
            if number in profile.get("raised", ()):
                return False, "an evaluation raised or ran off the main thread"
            records = profile["records"].get(number)
            if records is None:
                return True, "changed expression never evaluated"
            new_root = counterpart(root, change.old_tree, change.new_tree)
            ok, why = prove_expression(root, new_root, id(root) in change.conditions, records)
            return ok, why

        if statement is not None and self._function_body(chain, statement):
            body = getattr(statement, "body", None)
            end = statement.end_lineno if owner is statement or not body else body[0].lineno - 1
            imported = self.import_lines.get(change.path, set())
            if any(n in imported for n in range(statement.lineno, end + 1)):
                return False, "changed statement runs at import"
            if not self.covers(test, change.path, statement.lineno, end):
                return True, "changed statement never executed"
        return False, "no lemma applies"

    @staticmethod
    def _function_body(chain, statement):
        for i, node in enumerate(chain):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rest = chain[i + 1 :]
                if rest and any(rest[0] is s for s in node.body):
                    return True
        return False


def load_profile_dir(path):
    profiles = {}
    for file in Path(path).glob("*.pkl"):
        with open(file, "rb") as f:
            profiles[int(file.stem)] = pickle.load(f)
    return profiles


def load_packaged(data_dir, sqlglot_dir, tests_list):
    data_dir = Path(data_dir)
    if str(sqlglot_dir) not in sys.path:
        sys.path.insert(1, str(sqlglot_dir))
    by_number = {t["number"]: t for t in tests_list}
    number_of_id = {t["id"]: t["number"] for t in tests_list}
    with gzip.open(data_dir / "heavy_map.json.gz", "rt") as f:
        heavy = json.load(f)
    numbers = [number_of_id.get(test_id) for test_id in heavy["tests"]]

    def convert(indices):
        return [numbers[i] for i in indices if numbers[i] is not None]

    coverage = {
        "lines": {f: {line: convert(v) for line, v in m.items()} for f, m in heavy["lines"].items()},
        "import_lines": heavy["import_lines"],
        "fixtures": {f: convert(v) for f, v in heavy["fixtures"].items()},
    }
    with open(data_dir / "profiles.pkl", "rb") as f:
        by_id = pickle.load(f)
    profiles = {number_of_id[i]: p for i, p in by_id.items() if i in number_of_id}
    with gzip.open(data_dir / "target_index.json.gz", "rt") as f:
        index = json.load(f)
    prover = Prover(sqlglot_dir, by_number, coverage, profiles, index)
    union_path = data_dir / "union.pkl"
    if union_path.exists():
        with open(union_path, "rb") as f:
            prover.attach_union(pickle.load(f), number_of_id)
    return prover


def load_prover(args):
    tests_by_number = {t["number"]: t for t in json.loads(Path(args.tests).read_text())["tests"]}
    return Prover(
        args.sqlglot,
        tests_by_number,
        json.loads(Path(args.coverage).read_text()),
        load_profile_dir(args.profiles),
        json.loads(Path(args.index).read_text()),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    parser.add_argument("--tests")
    parser.add_argument("--sqlglot", required=True)
    parser.add_argument("--coverage", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--selection", help="selector output to filter; omitted means report only")
    args = parser.parse_args()
    args.tests = args.tests or str(Path(args.data) / "tests.json")
    prover = load_prover(args)

    if args.selection:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from selector import pack

        duration = {n: round(t["seconds"] * 1000) for n, t in prover.tests.items()}
        for raw in Path(args.selection).read_text().splitlines():
            line = json.loads(raw)
            patch = (Path(args.data) / "commits" / f"{line['commit']}.patch").read_text()
            verdict = prover.verdict(patch)
            selected = {n for worker in line["workers"] for n in worker}
            if verdict["noop"]:
                kept = set()
            else:
                kept = selected - set(verdict["safe"])
            dropped = sorted(selected - kept)
            print(json.dumps({"commit": line["commit"], "workers": pack(kept, duration)}))
            print(f"{line['commit']}: dropped {dropped} {verdict['notes']}", file=sys.stderr)
        return

    for patch in sorted((Path(args.data) / "commits").glob("*.patch")):
        verdict = prover.verdict(patch.read_text())
        print(patch.stem, json.dumps({"noop": verdict["noop"], "safe": verdict["safe"], "notes": verdict["notes"]}))


if __name__ == "__main__":
    main()
