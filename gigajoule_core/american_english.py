"""American spellings, in one place, and the check that holds the tree to them.

Role: code hygiene
Reads: the source of any Python file handed to it
Writes: nothing
Can move tokens: no
Live-safe: yes

Behavior. Authorized. Canceled, labeled, modeling, judgment, gray, center,
analyze. `SPELLINGS` is the list, in one place, and everything that needs it
derives from there rather than keeping its own copy -- two copies of one
vocabulary agree on the day they are written and drift from then on.

THE COUNTEREXAMPLES ARE DELIBERATELY NOT WRITTEN OUT HERE. The first draft of
this docstring read "Behavior, not <the other spelling>", which is the natural
way to state a spelling rule and which this module's own check immediately
flagged -- correctly, because the forbidden word was sitting in prose. The
temptation at that point is to exempt this file from its own gate. That would
be a suppression standing in for a fix, and it would put the vocabulary in the
tree twice: once in `SPELLINGS`, where it is data, and once in a paragraph,
where it rots. Both spellings of every word are one `SPELLINGS` lookup away and
that is the only place either belongs.

PROSE ONLY, AND NEVER IDENTIFIERS. The boundary is the same one `cycle.py`
draws for the micro sign: a convention governs what a human READS, never what a
machine PARSES. Renaming a variable, a column, a keyword argument or a field in
somebody else's API to satisfy a spelling preference is a behavior change with
no reader benefit, and on a system that writes a ledger it is a behavior change
nobody asked for. So this walks COMMENTS and DOCSTRINGS, and nothing else.

THIS IS A CLEAN GATE, NOT A RATCHET. Same reasoning as `module_header.py`: a
baseline is for stopping a measured defect class from growing on the day you
first measure it, and on the day this repo was created there was nothing to
measure. Starting with a baseline file would be choosing to start with a
backlog.
"""

from __future__ import annotations

import re
from pathlib import Path

from gigajoule_core.source_text import comments_of, docstrings_of

# British -> American. Kept short and specific on purpose: a long list of near
# misses produces false positives in quoted material, and a check that cries
# wolf is a check somebody disables.
SPELLINGS = {
    "behaviour": "behavior",
    "behavioural": "behavioral",
    "authorise": "authorize",
    "authorised": "authorized",
    "authorises": "authorizes",
    "authorisation": "authorization",
    "cancelled": "canceled",
    "cancelling": "canceling",
    "labelled": "labeled",
    "labelling": "labeling",
    "modelling": "modeling",
    "normalise": "normalize",
    "normalised": "normalized",
    "normalises": "normalizes",
    "materialise": "materialize",
    "materialised": "materialized",
    "materialises": "materializes",
    "organisation": "organization",
    "recognise": "recognize",
    "recognised": "recognized",
    "recognises": "recognizes",
    "analyse": "analyze",
    "analysed": "analyzed",
    # "analyses" is deliberately ABSENT. It is the British third-person verb, but
    # it is equally the correct American plural of "analysis" -- "the analyses
    # agreed" is not a defect -- so a rule that flagged it would fire on correct
    # prose. A check that cries wolf is a check somebody disables, which costs
    # more than the handful of real hits it would catch. The same reasoning keeps
    # "practises"/"practices" out: one spelling is a verb and the other a noun in
    # both dialects.
    "judgement": "judgment",
    "colour": "color",
    "centre": "center",
    "grey": "gray",
    "licence": "license",
    "defence": "defense",
    "metre": "meter",
    "litre": "liter",
}

_PATTERN = re.compile(r"\b(" + "|".join(sorted(SPELLINGS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def prose_of(path: Path) -> list[tuple[int, str]]:
    """Every comment and docstring in a file, as (line number, text).

    Both come from `source_text`, which is the one place in this repo that knows
    how to find either. A regex over the raw source would also match an
    identifier, which is the thing this check must never flag.
    """
    return [*comments_of(path), *docstrings_of(path)]


def british_spellings_in(path: Path) -> list[tuple[int, str, str]]:
    """Every British spelling in a file's prose, as (line, found, preferred)."""
    hits: list[tuple[int, str, str]] = []
    for lineno, text in prose_of(path):
        for match in _PATTERN.finditer(text):
            word = match.group(1)
            hits.append((lineno, word, SPELLINGS[word.lower()]))
    return hits
