"""Round-trip loss: what a battery gives back and where the rest went.

Role: test (storage efficiency)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes
"""

from __future__ import annotations

import pytest

from gigajoule_core.storage import BASIS_POINTS, StorageError, check_efficiency, release_joules


def test_the_split_always_conserves():
    """Released plus lost is the input, exactly, at every efficiency.

    This is the property the discharge batch depends on: if the two did not sum
    to the drawdown, the batch would not balance and the ledger would refuse it
    with a much less useful message than this test's name.
    """
    for stored in (0, 1, 7, 999, 3_600_000, 10**12 + 1):
        for bp in (1_000, 5_000, 8_800, 9_999, BASIS_POINTS):
            released, lost = release_joules(stored, bp)
            assert released + lost == stored, f"{stored}J at {bp}bp did not conserve"
            assert released >= 0 and lost >= 0


def test_eighty_eight_percent_of_a_kilowatt_hour():
    """The worked example from the module docstring, by value."""
    released, lost = release_joules(3_600_000, 8_800)
    assert released == 3_168_000
    assert lost == 432_000


def test_rounding_always_favors_the_loss_account():
    """Floor division, and the direction is the point.

    A battery must never release more than physics allows. Rounding the other
    way means one cycled often enough emits energy that was never generated,
    which is a small number right up until somebody notices it is systematically
    wrong in the operator's favor.
    """
    released, lost = release_joules(7, 8_800)  # 6.16 exactly
    assert released == 6
    assert lost == 1


def test_a_perfect_battery_loses_nothing():
    """10000bp is the boundary, and it must be admitted rather than rejected."""
    assert release_joules(100, BASIS_POINTS) == (100, 0)


def test_an_efficiency_outside_the_range_is_refused():
    for bad in (0, -1, BASIS_POINTS + 1):
        with pytest.raises(StorageError, match="basis points"):
            release_joules(100, bad)


def test_a_fraction_entered_where_basis_points_were_meant_is_caught():
    """88 means 0.88%, which would send 99% of every charge to LOSS.

    Registration-time guard rather than a physical law: flow and thermal storage
    genuinely run below lithium-ion, so the bar sits where no real asset lives
    rather than where the good ones do.
    """
    with pytest.raises(StorageError, match="basis points"):
        check_efficiency(88)
    check_efficiency(8_800)
    check_efficiency(BASIS_POINTS)


def test_a_negative_drawdown_is_refused():
    with pytest.raises(StorageError, match="negative"):
        release_joules(-1, 8_800)
