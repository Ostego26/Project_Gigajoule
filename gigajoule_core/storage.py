"""What a battery gives back, and where the rest of it went.

Role: decision (pure functions, no I/O)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

A BATTERY DESTROYS ENERGY, AND A LEDGER THAT CONSERVES EXACTLY HAS TO SAY SO.

Put 100J into a lithium-ion cell and roughly 88J comes back out. The other 12J
left as heat. That is not an accounting adjustment or a fee -- it is physics, it
is the single largest reason storage is expensive, and a registry that quietly
released 100J would be issuing 12J of tokens backed by nothing.

So the loss is a LEDGER ENTRY, credited to the LOSS account, and conservation
stays exact: minted == circulating + stored + retired + lost. The alternative --
letting discharge return slightly less than it took and calling the difference
rounding -- is how a conservation check acquires a tolerance, and a tolerance is
the width of the discrepancy nobody investigates.

EFFICIENCY IS INTEGER BASIS POINTS. 8800 is 88.00%. Same argument as units.py:
this number multiplies every joule that passes through an asset, and its output
is compared against an exact invariant. A float would put a residue exactly
where the check is exact.

THE DIVISION FLOORS, AND THE DIRECTION IS DELIBERATE. Any remainder goes to LOSS
rather than to the recipient, so the asset can never release more than physics
allows -- at worst it under-delivers by a joule. Rounding the other way would
mean a battery cycled often enough emits energy that was never generated, which
is exactly the failure this module exists to make impossible. One joule per
cycle sounds negligible; it is negligible right up until somebody notices the
number is systematically wrong in the direction that favors the operator.
"""

from __future__ import annotations

BASIS_POINTS = 10_000

# A round-trip efficiency below this is almost certainly a units error -- a
# fraction where basis points were meant (0.88 entered as 88 is 0.88%), which
# would send 99% of every charge to LOSS. Registered as a REGISTRATION-time
# guard, not a physical law: flow batteries and thermal storage genuinely run
# lower than lithium-ion, so the bar is set where no real asset lives rather
# than where the good ones do.
IMPLAUSIBLY_LOW_BP = 1_000


class StorageError(ValueError):
    """A storage parameter that cannot describe a real asset."""


def release_joules(stored_joules: int, round_trip_bp: int) -> tuple[int, int]:
    """Split stored energy into what comes out and what is lost.

    Returns `(released, lost)`, which always sum to exactly `stored_joules` --
    asserted here rather than trusted, because that sum is what keeps the
    discharge batch balanced and a batch that does not balance is refused at the
    ledger with a much less useful message.
    """
    if stored_joules < 0:
        raise StorageError(f"cannot release a negative amount: {stored_joules}J")
    if not 0 < round_trip_bp <= BASIS_POINTS:
        raise StorageError(
            f"round-trip efficiency must be in (0, {BASIS_POINTS}] basis points, "
            f"got {round_trip_bp}. 8800 means 88.00%."
        )
    released = stored_joules * round_trip_bp // BASIS_POINTS
    lost = stored_joules - released
    if released + lost != stored_joules:
        raise AssertionError(
            f"release split does not conserve: {released} + {lost} != {stored_joules}"
        )
    return released, lost


def check_efficiency(round_trip_bp: int) -> None:
    """Refuse an efficiency that is almost certainly a units error.

    Runs at asset registration rather than at every discharge: the value is
    written once and read forever, so the cheap place to catch a mistyped one is
    where it enters.
    """
    if round_trip_bp <= 0 or round_trip_bp > BASIS_POINTS:
        raise StorageError(
            f"round-trip efficiency must be in (0, {BASIS_POINTS}] basis points, "
            f"got {round_trip_bp}. 8800 means 88.00%."
        )
    if round_trip_bp < IMPLAUSIBLY_LOW_BP:
        raise StorageError(
            f"round-trip efficiency of {round_trip_bp} basis points is {round_trip_bp / 100}%, "
            "which no real storage asset achieves. The usual cause is a fraction "
            "entered where basis points were meant: 0.88 efficiency is 8800, not 88."
        )
