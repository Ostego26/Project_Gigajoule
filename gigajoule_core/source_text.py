"""Pulling comments and docstrings out of a Python file, correctly, in one place.

Role: code hygiene
Reads: the source of any Python file handed to it
Writes: nothing
Can move tokens: no
Live-safe: yes

A LINE-BASED SEARCH FOR A COMMENT IS WRONG, AND IT IS WRONG IN THE DIRECTION
THAT MAKES A CLEAN FILE LOOK DIRTY. That is not a hypothetical: the suppression
census in this repo was first written as a regex over raw lines, and it
immediately counted this line --

    assert _DIRECTIVE.search("x = 1  # noqa: S101") is not None

-- as a suppression. It is a string literal inside a test that exists to prove
the parser works. A repo that had just REMOVED its last suppression would be
scored as still carrying one, and the fix would look like a regression.

So there is exactly one comment finder and one docstring finder, here, built on
`tokenize` and `ast` respectively, and everything that needs either imports it.
Two implementations of "find the comments" would agree on the day they were
written and drift from then on, and the drift would be invisible -- each would
look correct in its own file, and nothing would fail until two checks disagreed
about the same line.
"""

from __future__ import annotations

import ast
import contextlib
import io
import tokenize
from pathlib import Path

_DOC_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def read_source(path: Path) -> str | None:
    """A file's text, or None if it cannot be read as UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def comments_of(path: Path) -> list[tuple[int, str]]:
    """Every real comment in a file, as (line number, text).

    Real means a COMMENT token: `#` that the tokenizer saw as starting a comment,
    not the characters `#` and `noqa` appearing inside a string. See the module
    docstring for the census this distinction rescued.
    """
    source = read_source(path)
    if source is None:
        return []
    found: list[tuple[int, str]] = []
    # A file that does not tokenize still has an AST worth reading elsewhere, and
    # a partial answer beats aborting the whole sweep at the first broken file.
    with contextlib.suppress(tokenize.TokenError, IndentationError, SyntaxError):
        found.extend(
            (token.start[0], token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        )
    return found


def docstrings_of(path: Path) -> list[tuple[int, str]]:
    """Every module, class and function docstring, as (line number, text)."""
    source = read_source(path)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        (getattr(node, "lineno", 1), doc)
        for node in ast.walk(tree)
        if isinstance(node, _DOC_NODES) and (doc := ast.get_docstring(node))
    ]
