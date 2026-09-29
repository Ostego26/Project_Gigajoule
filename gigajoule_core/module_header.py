"""The header every module in this repo declares itself with, and its parser.

Role: code hygiene
Reads: the source of any Python file handed to it
Writes: nothing
Can move tokens: no
Live-safe: yes

EVERY MODULE CARRIES THIS BLOCK IN ITS DOCSTRING:

    Role: ledger operations
    Reads: gj_meter_reading, gj_account
    Writes: gj_ledger_entry
    Can move tokens: YES
    Live-safe: no

The two that earn their place are the last two, and they are the reason this is
a mechanical check rather than a convention. Before running anything against a
live ledger an operator has to know whether it can move tokens and whether it is
safe to run at all, and the only answer that survives contact with a growing
codebase is one stated in the file itself.

`Can move tokens` is this project's version of a capability declaration: a
module that writes to `gj_ledger_entry` changes what someone owns. The field is
adapted from a sibling system where it read `Can send orders`; the wording
follows the domain, the purpose does not.

THIS IS A CLEAN GATE, NOT A RATCHET, AND IT STAYS ONE. There is no baseline
file, no per-file exemption list and no allowance for a module that predates the
rule, because on the day this repo was created no module did. A baseline is
legitimate for exactly one thing -- stopping a measured defect class from
GROWING on the day you first measure it -- and a new repo has nothing to
measure. Adding one here would mean choosing to start with a backlog.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Order matters: this is the order the fields are written in, and the test
# reports them in it so a diff between two files reads consistently.
REQUIRED_FIELDS = ("Role", "Reads", "Writes", "Can move tokens", "Live-safe")


def module_docstring(path: Path) -> str | None:
    """Return a file's module docstring, or None if it has none or cannot parse.

    A syntax error returns None rather than raising: this runs over every file
    in the tree, and a parse failure should be reported as a missing header by
    the caller alongside the others, not abort the whole sweep at the first
    broken file.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    return ast.get_docstring(tree)


def missing_header_fields(path: Path) -> list[str]:
    """Which of REQUIRED_FIELDS this file does not declare.

    Empty list means a complete header. A file with no docstring at all returns
    every field, which is correct and is also why the test can report "absent"
    and "partial" from one function.

    FIELDS ARE COUNTED, NOT PRESENCE, and that distinction is the whole point.
    A partial header is the worse kind of defect: it reads as complete at a
    glance, so nobody looks further, and in practice the field left off is the
    last one -- which here is `Live-safe`, the single question an operator most
    needs answered before running something against a live ledger.
    """
    doc = module_docstring(path)
    if not doc:
        return list(REQUIRED_FIELDS)
    return [field for field in REQUIRED_FIELDS if f"{field}:" not in doc]
