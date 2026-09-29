"""The declared package list matches the tree, so setuptools never has to guess.

Role: test (code hygiene)
Reads: pyproject.toml and the top level of the repository
Writes: nothing
Can move tokens: no
Live-safe: yes

WHY THIS TEST EXISTS, with the failure it was written for.

`pyproject.toml` originally declared no packages at all, which leaves setuptools
to discover them. Its flat-layout discovery treats every top-level directory as
a candidate, found `gigajoule_core` and `runtime`, refused to guess between
them, and failed the install before it began:

    error: Multiple top-level packages discovered in a flat-layout:
    ['runtime', 'gigajoule_core']

Reported from a fresh clone on 2026-09-29 by the first person to run the
README's own install line. It did not reproduce in the working tree it was
written in, because there `runtime/` had been created by a test run rather than
by the checkout -- the defect needed a clean clone to appear, which is exactly
the situation a new contributor is always in.

WHAT IS ACTUALLY PINNED HERE, because "the install worked once" is not it. The
future version of this failure is somebody adding a second real package -- a web
service, a worker, a client library -- and not declaring it. The install then
breaks for everyone on the next clone, with an error naming setuptools rather
than the commit that caused it. So the assertion is a DERIVATION CHECK: the
declared list and the packages actually present in the tree must be the same
set, and it fails from either side, whichever one somebody edits.

It is deliberately cheap. Proving this by running a real build would cost a
subprocess and several seconds on every suite run to check a fact that reading
two lists answers in milliseconds, and the cost of a thorough test almost always
lives in the scaffolding rather than in the thing being proven.
"""

from __future__ import annotations

import tomllib

from gigajoule_core.repo_tree import REPO_ROOT

# Directories at the top level that are deliberately NOT packages. Each is here
# for a stated reason, so that adding to this set is an argument rather than a
# reflex.
NOT_PACKAGES = {
    # Runtime state: the authority database lands here. `state/.gitkeep` is
    # tracked so the directory exists in every checkout, which is precisely why
    # setuptools sees it and why this whole module exists. It must never be
    # packaged -- installing it would ship an empty ledger into site-packages.
    "runtime",
    # Tests are not shipped. setuptools' own flat-layout convention already skips
    # this name; it is listed anyway so the set below reads as complete.
    "tests",
}


def declared_packages() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    return config.get("tool", {}).get("setuptools", {}).get("packages", [])


def packages_in_tree() -> set[str]:
    """Top-level directories that are importable Python packages."""
    return {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "__init__.py").exists()
    }


def test_the_declared_packages_are_exactly_the_ones_in_the_tree():
    """Fails from either side: an undeclared package, or a declared phantom."""
    declared = set(declared_packages())
    present = packages_in_tree()
    assert declared == present, (
        f"pyproject declares {sorted(declared)} but the tree has {sorted(present)}. "
        "An undeclared package breaks `pip install -e .` on the next clean clone; "
        "a declared one that does not exist breaks the build immediately."
    )


def test_packages_is_declared_at_all():
    """The empty-list case, which is the exact state that shipped the defect.

    An absent or empty `packages` is what hands discovery back to setuptools, and
    discovery is what fails. Asserted separately from the set comparison above
    because an empty declaration against an empty tree would satisfy that one.
    """
    assert declared_packages(), (
        "tool.setuptools.packages is missing or empty, so setuptools will "
        "auto-discover and fail on the top-level directories that are not packages"
    )


def test_runtime_state_is_not_importable_as_a_package():
    """The specific directory that caused it, pinned by name.

    If somebody ever adds `runtime/__init__.py`, the set comparison above would
    start demanding that `runtime` be DECLARED -- which would make the install
    succeed by shipping runtime state into site-packages. That is the wrong fix,
    so the wrong fix is blocked here rather than left to a reviewer to notice.
    """
    assert "runtime" in NOT_PACKAGES
    assert not (REPO_ROOT / "runtime" / "__init__.py").exists(), (
        "runtime/ holds the authority database, not code. Making it a package "
        "would install an empty ledger into site-packages."
    )


def test_the_tree_scan_actually_finds_something():
    """Mutation check: a scan that returns nothing would pass every test above."""
    assert "gigajoule_core" in packages_in_tree()
