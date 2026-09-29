"""Energy quantities, and the one decision the rest of this system inherits.

Role: units (pure functions, no I/O)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

THE LEDGER UNIT IS THE INTEGER JOULE. Not the kilowatt-hour, not the
gigajoule the project is named for, and above all not a float of either.

WHY THIS IS WRITTEN AT LENGTH: it is the decision every stored balance
inherits, and reversing it later means rewriting the ledger rather than
changing a function. A reader who does not know the reasoning will eventually
"simplify" a float in somewhere, and nothing will fail on the day they do.

A token here represents energy that was physically generated, and the central
invariant is conservation: joules minted == joules held + joules retired,
exactly, forever. "Exactly" is only available in integer arithmetic. In IEEE-754
binary floating point 0.1 kWh is not representable, so a ledger denominated in
float kWh accumulates a residue on every single operation, and the conservation
check then has to be written with a tolerance. A tolerance is a hole: it is the
width of the discrepancy nobody will investigate, it grows with transaction
count, and on a system whose entire product is "this token is backed by real
energy" it is the one number an auditor will ask about first.

WHY JOULES AND NOT GIGAJOULES, given the project name. The gigajoule is the
DISPLAY unit -- it is the size a human trades in, the same way a currency
displays dollars. The STORAGE unit has to be the smallest quantity any meter
can report, because storing in GJ would force a division at write time and a
division is where the residue comes from. One gigajoule is 10**9 joules, so the
conversion to display is exact and lossless in the direction that matters.

WHY NOT WATT-HOURS AS THE BASE, which is the industry's habit. The joule is the
SI unit and, critically, energy = power x time falls out of it with no constant:
a 1,000 W source running for 60 s produced exactly 60,000 J. That identity is
what `attestation.plausible_joules_ceiling()` uses to reject a meter reporting
more energy than its nameplate can physically produce. Expressed in watt-hours
the same check carries a 3,600 and somebody eventually writes it as 3,000.

EXACTNESS OF THE kWh CONVERSION, because it decides where rounding is allowed.
1 kWh == 3,600,000 J exactly, so a kWh figure with up to FIVE decimal places is
an exact whole number of joules: 10**-5 kWh == 36 J, on the nose. SIX is where
it stops -- 10**-6 kWh is 3.6 J, which is not a whole number of anything -- and
past that the function rounds half-to-even and says so rather than truncating
silently. Banker's rounding because a systematic bias applied to millions of
intervals is a slow leak in one direction, which is exactly the shape of defect
this module exists to prevent.

THIS DOCSTRING SAID SIX FOR ABOUT TEN MINUTES, and the correction is worth more
than the number. Six is the answer you get by reasoning that 3.6e6 carries six
digits after the leading 3; it is wrong because the trailing digit of 3.6 is
itself a tenth, so the last place divides into joules and leaves 0.6 over. The
figure was written into the module, into `EXACT_KWH_PLACES`, and into the test's
own name before `test_six_decimal_places_of_kwh_are_exact` ran and returned
444,442 where the exact arithmetic gives 444,441.6. A plausible derivation, an
untested claim stated in the register of a measurement, and a two-line test that
settled it. That is the entire argument for writing the test that would show the
claim false rather than re-reading the reasoning that produced it.

In practice none of this reaches a meter: revenue-grade metering reports at
10**-3 kWh or coarser, five orders inside the boundary, so `kwh_to_joules` is
lossless on every real input and the rounding path exists for submissions that
should be questioned rather than for ones that should be trusted.

DECIMAL, NEVER FLOAT, AT THE BOUNDARY. `kwh_to_joules` accepts str / int /
Decimal and REFUSES float. That refusal is deliberate and is not defensiveness:
by the time a float reaches this function the precision is already gone, and
accepting it would let the caller believe a conversion was exact when the input
never was. A meter payload arrives as text; keep it as text until it is a
Decimal.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# The three conversions this system needs, as exact integers. Each is a
# definition rather than a measurement, which is why they are written out
# instead of derived from one another: a reader checking one against a
# reference does not have to evaluate an expression to do it.
JOULES_PER_KWH = 3_600_000
JOULES_PER_MWH = 3_600_000_000
JOULES_PER_GJ = 1_000_000_000

# kWh figures with at most this many decimal places convert to an exact whole
# number of joules; 10**-5 kWh is exactly 36J. Beyond it `kwh_to_joules` rounds
# half-to-even and says so. Five rather than six -- see the module docstring for
# why six is the plausible wrong answer and what caught it.
EXACT_KWH_PLACES = 5

_DISPLAY_GJ_PLACES = Decimal("0.000001")


class EnergyValueError(ValueError):
    """A quantity that cannot be a ledger amount.

    Raised rather than returning None or zero because every caller of this
    module is about to write a row that claims energy exists. A silent zero
    would mint nothing and report success, which is the failure shape this
    project's rules single out: the caller cannot tell a refusal from a real
    answer.
    """


def kwh_to_joules(kwh: str | int | Decimal) -> int:
    """Convert a meter's kWh figure to integer joules.

    Exact for any input with at most EXACT_KWH_PLACES decimal places, which
    covers every revenue-grade meter in service. Past that, rounds half-to-even
    and the result is documented as inexact -- see this module's docstring for
    why the bias direction matters.

    Floats are REFUSED. A float argument means precision was already lost
    upstream, and accepting it here would launder an inexact input into an
    exact-looking integer.
    """
    if isinstance(kwh, float):
        raise EnergyValueError(
            f"kwh_to_joules refuses float ({kwh!r}); pass str or Decimal. "
            "A float kWh has already lost the precision this ledger needs -- "
            "keep the meter payload as text until it is a Decimal."
        )
    try:
        value = Decimal(kwh)
    except (InvalidOperation, TypeError) as exc:
        raise EnergyValueError(f"not a decimal quantity: {kwh!r}") from exc
    if not value.is_finite():
        raise EnergyValueError(f"not a finite quantity: {kwh!r}")
    if value < 0:
        raise EnergyValueError(
            f"negative energy: {kwh!r}. A meter interval cannot produce "
            "negative joules; net export/import is two readings, not one "
            "signed reading, so that the provenance of each direction is "
            "attestable separately."
        )
    return int((value * JOULES_PER_KWH).to_integral_value(rounding=ROUND_HALF_EVEN))


def mwh_to_joules(mwh: str | int | Decimal) -> int:
    """Convert MWh to integer joules. Same refusals as `kwh_to_joules`."""
    if isinstance(mwh, float):
        raise EnergyValueError(
            f"mwh_to_joules refuses float ({mwh!r}); pass str or Decimal."
        )
    return kwh_to_joules(Decimal(mwh) * 1000)


def joules_to_kwh(joules: int) -> Decimal:
    """Exact kWh for a whole number of joules. Never used for storage."""
    _require_int(joules, "joules_to_kwh")
    return Decimal(joules) / JOULES_PER_KWH


def joules_to_gj(joules: int) -> Decimal:
    """Exact gigajoules for a whole number of joules. Never used for storage."""
    _require_int(joules, "joules_to_gj")
    return Decimal(joules) / JOULES_PER_GJ


def format_gj(joules: int) -> str:
    """Render joules as the display unit, e.g. `3.600000GJ`.

    No space before the unit, matching the timing convention in `cycle.py`:
    it is one quantity, so it reads as one token, and a space breaks column
    alignment in every report that prints a list of these.
    """
    _require_int(joules, "format_gj")
    quantized = joules_to_gj(joules).quantize(_DISPLAY_GJ_PLACES)
    return f"{quantized}GJ"


def _require_int(joules: object, caller: str) -> None:
    """Reject a non-integer amount at the point it enters a display path.

    `bool` is excluded explicitly: it is a subclass of `int` in Python, so
    `isinstance(True, int)` is True and `format_gj(True)` would otherwise
    render as one joule rather than raising.
    """
    if isinstance(joules, bool) or not isinstance(joules, int):
        raise EnergyValueError(
            f"{caller} takes integer joules, got {type(joules).__name__}: {joules!r}"
        )
