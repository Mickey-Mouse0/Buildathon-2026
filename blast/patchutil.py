"""Read unified diffs: which old lines each file touches, and the file's new text."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileChange:
    def __init__(self, old_path, new_path):
        self.old_path = old_path  # None for a new file
        self.new_path = new_path  # None for a deleted file
        self.hunks = []  # list of (old_start, old_count, lines)

    @property
    def path(self):
        return self.new_path or self.old_path

    def removed_lines(self):
        """Old line numbers that were deleted or replaced."""
        out = set()
        for start, _, lines in self.hunks:
            o = start
            for ln in lines:
                if ln.startswith("-"):
                    out.add(o)
                    o += 1
                elif ln.startswith(" ") or ln == "":
                    o += 1
        return out

    def insert_points(self):
        """Old line numbers that new lines were inserted in front of."""
        out = set()
        for start, _, lines in self.hunks:
            o = start
            for ln in lines:
                if ln.startswith("+"):
                    out.add(o)
                elif ln.startswith("-") or ln.startswith(" ") or ln == "":
                    o += 1
        return out

    def apply(self, old_text):
        """New text of the file, or None when the diff does not fit."""
        if self.new_path is None:
            return ""
        old = old_text.splitlines(keepends=True) if old_text else []
        out = []
        pos = 0  # index into old
        for start, count, lines in self.hunks:
            begin = start - 1 if count != 0 else start
            if begin < pos or begin > len(old):
                return None
            out.extend(old[pos:begin])
            pos = begin
            prev_added = False
            for ln in lines:
                if ln.startswith("\\"):
                    # "\ No newline at end of file" applies to the previous line
                    if out and out[-1].endswith("\n") and prev_added:
                        out[-1] = out[-1][:-1]
                    continue
                tag, body = (ln[0], ln[1:]) if ln else (" ", "")
                if tag == " ":
                    if pos >= len(old) or old[pos].rstrip("\r\n") != body:
                        return None
                    out.append(old[pos])
                    pos += 1
                    prev_added = False
                elif tag == "-":
                    if pos >= len(old) or old[pos].rstrip("\r\n") != body:
                        return None
                    pos += 1
                    prev_added = False
                elif tag == "+":
                    out.append(body + "\n")
                    prev_added = True
        out.extend(old[pos:])
        return "".join(out)


def parse(patch_text):
    files = []
    cur = None
    hunk = None
    old_path = None
    in_header = True
    for ln in patch_text.splitlines():
        if ln.startswith("diff --git "):
            cur, hunk, in_header = None, None, True
            continue
        if in_header and ln.startswith("--- "):
            p = ln[4:].split("\t")[0]
            old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if in_header and ln.startswith("+++ "):
            p = ln[4:].split("\t")[0]
            new_path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            cur = FileChange(old_path, new_path)
            files.append(cur)
            in_header = False
            continue
        m = HUNK.match(ln)
        if m and cur is not None:
            count = int(m.group(2)) if m.group(2) is not None else 1
            hunk = (int(m.group(1)), count, [])
            cur.hunks.append(hunk)
            continue
        if hunk is not None and (ln[:1] in (" ", "-", "+", "\\") or ln == ""):
            hunk[2].append(ln)
        elif hunk is not None:
            in_header = True
    return files
