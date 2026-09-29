"""All English in this repo is American English, in prose and never in identifiers.

Role: test (code hygiene)
Reads: the source of every Python file in the repo
Writes: nothing
Can move tokens: no
Live-safe: yes

A CLEAN GATE, for the same reason as the header check: on the day this repo was
created nothing violated the rule, so there is nothing to baseline.

The check walks comments and docstrings only. An identifier, a column name, a
keyword argument or a third-party API field is never touched, because a
convention governs what a human reads and never what a machine parses --
renaming a column to satisfy a spelling preference is a behavior change on a
system that writes a ledger.
"""

from __future__ import annotations

from gigajoule_core.american_english import SPELLINGS, british_spellings_in
from gigajoule_core.repo_tree import repo_python_files


def test_prose_is_american_english():
    hits = {
        path.name: found for path in repo_python_files() if (found := british_spellings_in(path))
    }
    assert not hits, "British spellings in prose:\n" + "\n".join(
        f"  {name}: " + ", ".join(f"line {line}: {word} -> {preferred}" for line, word, preferred in found)
        for name, found in sorted(hits.items())
    )


def test_the_check_actually_finds_something(tmp_path):
    """Mutation check. A gate that passes because it looks at nothing is worse than none."""
    offender = tmp_path / "offender.py"
    offender.write_text('"""This describes the behaviour of a thing."""\n')
    found = british_spellings_in(offender)
    assert found == [(1, "behaviour", "behavior")]


def test_identifiers_are_never_flagged(tmp_path):
    """The boundary: prose is checked, code is not.

    The file seeded below uses British spellings in a variable name and in two
    keyword arguments. All three are code, and flagging them would make the only
    available fix a rename -- a behavior change, on a system that writes a
    ledger, bought for nothing a reader can see.

    The offending words are in the seeded source rather than in this sentence.
    Writing them here would fail this module's own gate, which is the same trap
    `american_english.py` records in its docstring: the file that states a rule
    is the one place a violation refutes itself.
    """
    code = tmp_path / "identifiers.py"
    code.write_text(
        '"""A module with a clean docstring."""\n'
        "behaviour_flag = 1\n"
        "def f(colour=None, centre=0):\n"
        "    return behaviour_flag\n"
    )
    assert british_spellings_in(code) == []


def test_a_comment_is_prose_and_is_checked(tmp_path):
    """Comments count. They are read by humans, which is the whole test."""
    commented = tmp_path / "commented.py"
    commented.write_text('"""Fine."""\n# this normalises the value\nx = 1\n')
    assert [hit[1] for hit in british_spellings_in(commented)] == ["normalises"]


def test_every_spelling_maps_to_a_different_word():
    """A self-mapping entry would make the check demand a word it already accepts."""
    assert all(british != american for british, american in SPELLINGS.items())
