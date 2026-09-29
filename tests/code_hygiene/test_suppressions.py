"""How many lint suppressions this repo has, and where. The answer is two.

Role: test (code hygiene)
Reads: the source of every Python file in the repo
Writes: nothing
Can move tokens: no
Live-safe: yes

NO BASELINE FILE, AND NO ROOM FOR ONE. A `noqa` is a claim that you checked what
the rule was warning about and found it did not apply; it is never a way to quiet
a finding you have not read. The way that degrades is well understood: a
suppression makes a check green, green reads as done, and a few hundred of them
later nobody can tell which ones were reasoned about.

So the census is pinned by VALUE rather than by a growing list. Two suppressions
exist, both in `repo_tree.py`, both on the one `subprocess.run` call that asks
git what it tracks:

  S603  the call takes a fixed argument list with shell=False and nothing
        interpolated from input.
  S607  `git` is resolved from PATH deliberately. Pinning an absolute path would
        make the walk work on one machine and fail on every other.

The cause cannot be removed without making the situation worse: the alternative
to asking git is walking the filesystem, which makes the denominator "whatever
happened to be lying around" -- and a count whose denominator drifts is the
defect the walk exists to prevent.

If a third suppression is ever genuinely warranted, this test is where the
argument for it gets written down.
"""

from __future__ import annotations

import re

from gigajoule_core.repo_tree import repo_python_files
from gigajoule_core.source_text import comments_of

# `#` immediately before `noqa`, which is what ruff itself requires. A looser
# pattern would also match the word quoted inside a docstring -- including the
# ones in this module's own header -- and would score a file that REMOVED a
# suppression as still carrying it.
_DIRECTIVE = re.compile(r"#\s*noqa")

ACCEPTED = {"repo_tree.py": 2}


def suppressions_in(path) -> int:
    """Count real directives, by walking comment TOKENS rather than raw lines.

    The line-based version of this function counted the string literal in
    `test_the_parser_does_not_count_the_word_in_prose` below as a suppression,
    which would have scored a repo that removed its last one as still carrying
    it -- an error in the direction that makes a fix look like a regression.
    """
    return sum(1 for _, text in comments_of(path) if _DIRECTIVE.search(text))


def test_the_suppression_census_is_exactly_what_is_argued_for():
    found = {path.name: n for path in repo_python_files() if (n := suppressions_in(path))}
    assert found == ACCEPTED, (
        f"suppression census changed: {found} != {ACCEPTED}. A new `noqa` needs an "
        "argument in this module's docstring, not an entry in a baseline file."
    )


def test_the_parser_does_not_count_a_directive_written_inside_a_string(tmp_path):
    """Mutation check, and the exact case that broke the first implementation.

    The file below has one string containing the directive text and no comment
    at all, so the honest answer is zero. A line-based counter answers one.
    """
    decoy = tmp_path / "decoy.py"
    decoy.write_text(
        '"""A module that only talks about suppressions."""\n'
        'MESSAGE = "write it as # noqa: S101 at the end of the line"\n'
    )
    assert suppressions_in(decoy) == 0

    real = tmp_path / "real.py"
    real.write_text('"""A module with one."""\nimport os  # noqa: F401\n')
    assert suppressions_in(real) == 1
