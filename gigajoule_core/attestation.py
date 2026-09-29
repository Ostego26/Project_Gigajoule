"""Whether a meter reading is physically possible, and who says it happened.

Role: decision (pure functions, no I/O)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

This module holds the checks that run BEFORE a reading is allowed to mint. It
is deliberately free of I/O so that each decision can be called with seeded
values and asserted on directly, rather than only by running the pipeline that
happens to reach it.

THE PHYSICAL CEILING IS THE CHECK THAT MATTERS. Energy = power x time, and in
SI that identity carries no constant: a source with a nameplate of 1,000 W
cannot produce more than 60,000 J in 60 s, whatever its meter reports. A
reading above that ceiling is one of three things -- a misconfigured nameplate,
a unit error upstream (kWh submitted where Wh was meant is a factor of 1,000),
or a deliberate overstatement -- and all three must be refused at the same
place, because the ledger cannot tell them apart after the fact and a minted
token is the hard thing to unwind.

THE CEILING IS A REFUSAL, NOT A CLAMP, and that is the whole design. Clamping
an impossible reading down to the ceiling would mint tokens from a reading
known to be wrong, and would do it silently: the row would look like every
other row, and the only record that anything was amiss would be a log line.
An impossible reading is evidence of a broken input, and the correct response
to a broken input is to refuse it and say which check refused it and by how
much -- so the operator can tell a nameplate typo from a unit error by reading
the ratio.

TOLERANCE, AND WHY THERE IS A LITTLE OF IT. `DEFAULT_TOLERANCE` exists because
a nameplate is a rating, not a hard physical limit: solar inverters clip above
nameplate under edge-of-cloud irradiance enhancement, and wind turbines
momentarily exceed rated power in gusts before pitch control responds. The
default of 5% is a REGISTERED FIGURE, not a measured one -- nobody here has
measured the true overshoot distribution for any fleet -- and it is stated as
such so the first person with production data replaces it with a measurement
rather than inheriting a guess. A factor-of-1,000 unit error is three orders
of magnitude clear of any tolerance this argument could justify, so the check
catches the failure it was built for at any plausible setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Registered, not measured. See the module docstring: replace this with a
# figure derived from production data the first time anyone has some.
DEFAULT_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class CeilingVerdict:
    """The outcome of the physical plausibility check on one reading.

    Carries the ratio as well as the verdict because the ratio is what tells
    an operator WHICH failure they have: ~1.0x with a small overshoot is a
    clipping inverter, ~1000x is a Wh/kWh unit error, and anything else wants
    a look at the nameplate. A bare True/False would throw that away and the
    investigation would start from nothing.
    """

    allowed: bool
    joules: int
    ceiling_joules: int
    ratio: Decimal

    def reason(self) -> str:
        """One line an operator can read off the screen without the source."""
        if self.allowed:
            return (
                f"within ceiling: {self.joules}J of {self.ceiling_joules}J "
                f"({self.ratio}x nameplate-seconds)"
            )
        return (
            f"REFUSED: {self.joules}J exceeds the physical ceiling of "
            f"{self.ceiling_joules}J by {self.ratio}x. ~1x means a clipping "
            "inverter or a low nameplate; ~1000x means a Wh/kWh unit error "
            "upstream; anything else, check the registered nameplate."
        )


def plausible_joules_ceiling(nameplate_watts: int, interval_seconds: int) -> int:
    """The most energy a source of this rating can produce in this long.

    Energy = power x time, in SI, with no conversion constant -- which is the
    reason `units.py` stores joules rather than watt-hours. Expressed in Wh
    this same check would carry a 3,600 and somebody would eventually write it
    as 3,000.
    """
    if nameplate_watts < 0:
        raise ValueError(f"negative nameplate: {nameplate_watts}")
    if interval_seconds <= 0:
        raise ValueError(
            f"interval must be positive, got {interval_seconds}s. A zero-length "
            "interval has a zero ceiling, so admitting one would refuse every "
            "reading against it for a reason that reads as a physics failure "
            "rather than the clock error it actually is."
        )
    return nameplate_watts * interval_seconds


def check_ceiling(
    joules: int,
    nameplate_watts: int,
    interval_seconds: int,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> CeilingVerdict:
    """Decide whether a reading is physically possible for its source.

    Returns a verdict rather than raising, because the caller is minting in a
    batch and needs to record WHY each refused reading was refused alongside
    the ones that passed. An exception here would cost the whole batch's
    diagnosis to learn one row's fate.
    """
    ceiling = plausible_joules_ceiling(nameplate_watts, interval_seconds)
    allowed_ceiling = int(Decimal(ceiling) * (1 + tolerance))
    ratio = (
        (Decimal(joules) / Decimal(ceiling)).quantize(Decimal("0.001"))
        if ceiling
        else Decimal("Infinity")
    )
    return CeilingVerdict(
        allowed=joules <= allowed_ceiling,
        joules=joules,
        ceiling_joules=ceiling,
        ratio=ratio,
    )
