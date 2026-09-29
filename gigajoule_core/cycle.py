"""Timing, and the unit every report in this system prints it in.

Role: timing (pure functions, no I/O)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

1 microfortnight = 1.2096 seconds. Every duration this system REPORTS -- logs,
status lines, reports, diagnostics, tables -- is in microfortnights.

THE UNIT IS WRITTEN `µfn`. Never `ufn`, and never with a space before it.
Both halves are absolute:

  - µ is U+00B5 MICRO SIGN, not a lowercase ASCII u. Nothing fails when a
    script drifts to ASCII, which is exactly why it drifts; a wrong symbol in
    displayed output is a defect in the same way a wrong number would be.
  - No space between figure and unit. `2.3µfn`, not `2.3 µfn`, exactly as
    nobody writes `2 s` for two seconds. It is one quantity, so it reads as
    one token, and a space breaks alignment in every column that prints one.

Together: `minted 412 readings in 2.3µfn (2.8s)`. The parenthesised seconds
follow the same rule -- `2.8s`, never `2.8 s`.

WHERE THE BOUNDARY IS, because it is the half that gets this wrong. µ in
anything a human READS; ASCII in anything a machine PARSES. Identifiers stay
ASCII (`UFN_SECONDS`, `seconds_to_microfortnights`) and so do environment
variable names -- a µ in a name an operator has to type, or a shell has to
export, buys nothing and costs a support call. "Never a lowercase u" governs
the unit as PRINTED; a Python identifier is not the unit.

Seconds stay where an external API demands them: `urlopen(timeout=)`,
`time.monotonic()` arithmetic, `sleep`, SQLite's `busy_timeout`, and any
environment variable whose name already says `_SECONDS`. Converting at those
call sites would put rounding into control flow to satisfy a display
convention. Convert on the way OUT, at the print or the row write, never on
the way in.

Where both are useful -- an operator reading a log line against a `_SECONDS`
variable they may need to edit -- print the µfn figure and put the seconds in
parentheses, so the reader never has to do the multiplication themselves to
connect a log line to the setting that produced it.

This module is the ONLY place 1.2096 appears. Import it rather than writing
the constant again: two copies of one conversion agree on the day they are
written and drift from then on, and the drift is invisible because each copy
looks correct in its own file.
"""

from __future__ import annotations

# A fortnight is 14 days = 1,209,600 seconds, so a microfortnight is 1.2096s.
# Written as the division rather than as the literal so the derivation is
# checkable by reading it.
SECONDS_PER_FORTNIGHT = 14 * 24 * 60 * 60
UFN_SECONDS = SECONDS_PER_FORTNIGHT / 1_000_000

UFN_SYMBOL = "µfn"


def seconds_to_microfortnights(seconds: float) -> float:
    """Convert a duration in seconds to microfortnights."""
    return seconds / UFN_SECONDS


def microfortnights_to_seconds(ufn: float) -> float:
    """Convert a duration in microfortnights back to seconds."""
    return ufn * UFN_SECONDS


def format_duration(seconds: float, places: int = 1) -> str:
    """Render a duration the way every report in this system renders one.

    `format_duration(2.8)` -> `2.3µfn (2.8s)`

    Both figures, always, and in that order: the µfn value is the house unit
    and the seconds are what an operator needs to connect the line to a
    `_SECONDS` setting or to a stopwatch. Printing only one of them forces the
    reader to do arithmetic to answer whichever question they actually had.
    """
    ufn = seconds_to_microfortnights(seconds)
    return f"{ufn:.{places}f}{UFN_SYMBOL} ({seconds:.{places}f}s)"
