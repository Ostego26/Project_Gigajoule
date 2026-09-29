"""Every module declares its role, its reach, and whether it can move tokens.

Role: test (code hygiene)
Reads: the source of every Python file in the repo
Writes: nothing
Can move tokens: no
Live-safe: yes

THIS IS A CLEAN GATE AND IT HAS NO BASELINE FILE. Every Python file in this
repo carries a complete header today, so there is nothing to tolerate and
nothing to ratchet down. A baseline is legitimate for exactly one purpose --
stopping a measured defect class from growing on the day you first measure it
-- and the way a baseline fails is that it makes the check green, green reads
as done, and the backlog stops being visible as work. Starting a new repository
with one would mean choosing to begin with a backlog.

If this test ever fails, the fix is the header, not an entry in a file.
"""

from __future__ import annotations

from gigajoule_core.module_header import REQUIRED_FIELDS, missing_header_fields
from gigajoule_core.repo_tree import enumeration_mode, repo_python_files


def test_every_module_declares_itself():
    """No exemptions, no baseline, no allowance for a file that predates the rule."""
    files = repo_python_files()
    assert files, "the walk found no Python files at all, which is itself the defect"

    incomplete = {
        path.relative_to(path.parents[len(path.parents) - 1]): missing
        for path in files
        if (missing := missing_header_fields(path))
    }
    assert not incomplete, (
        f"{len(incomplete)} of {len(files)} files are missing header fields "
        f"(enumeration: {enumeration_mode()}):\n"
        + "\n".join(f"  {path}: missing {fields}" for path, fields in sorted(incomplete.items()))
    )


def test_the_entry_point_is_covered_by_this_check():
    """The file an operator actually runs must not be the one that dodges the gate.

    `./gigajoule` has no `.py` suffix, so a walk globbing `*.py` would skip it --
    and it is the file whose `Can move tokens` line matters most, because it is
    the one a human invokes. Asserted by name so that a future change to the walk
    cannot quietly drop it.
    """
    names = {path.name for path in repo_python_files()}
    assert "gigajoule" in names, "the root entry point is not in the hygiene walk"


def test_the_fields_are_the_ones_an_operator_needs():
    """Pins the vocabulary, so a field cannot be dropped to make a file pass."""
    assert REQUIRED_FIELDS == ("Role", "Reads", "Writes", "Can move tokens", "Live-safe")


def test_a_file_with_no_docstring_reports_every_field(tmp_path):
    """Mutation check: the parser must not report a headerless file as complete."""
    empty = tmp_path / "headerless.py"
    empty.write_text("x = 1\n")
    assert missing_header_fields(empty) == list(REQUIRED_FIELDS)


def test_a_partial_header_is_caught_rather_than_passing_at_a_glance(tmp_path):
    """The worse defect: it reads as complete, so nobody looks further.

    The field most likely to be left off is the last one, which here is
    `Live-safe` -- the single question an operator has to answer before running
    something against a live ledger. Counting fields rather than checking for the
    presence of a header is what makes that visible.
    """
    partial = tmp_path / "partial.py"
    partial.write_text('"""Does a thing.\n\nRole: something\nReads: nothing\n"""\n')
    assert missing_header_fields(partial) == ["Writes", "Can move tokens", "Live-safe"]
