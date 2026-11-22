"""Parsed source shared by the selector's stages.

The analyzer, the prover and the TPC check's per-query analysis each need the syntax
tree of the pinned file and of the patched one. Nothing writes to these trees, so each
text is parsed once: the pinned file once per run, the patched one once per commit.
"""
import ast

_pinned = {}
_patched = {}


def pinned(path, text):
    """The tree of a file at the pinned commit (text is its content)."""
    hit = _pinned.get(path)
    if hit is None or hit[0] != text:
        hit = _pinned[path] = (text, ast.parse(text))
    return hit[1]


def patched(text):
    """ast.parse(text) for a patched file; raises SyntaxError like ast.parse."""
    tree = _patched.get(text)
    if tree is None:
        tree = ast.parse(text)
        if len(_patched) >= 4:
            _patched.clear()
        _patched[text] = tree
    return tree
