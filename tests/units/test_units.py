"""The unit conversions, and the refusals that keep float out of the ledger.

Role: test (units)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gigajoule_core.units import (
    EXACT_KWH_PLACES,
    JOULES_PER_GJ,
    JOULES_PER_KWH,
    EnergyValueError,
    format_gj,
    joules_to_gj,
    kwh_to_joules,
    mwh_to_joules,
)


def test_one_kwh_is_exactly_3_6_million_joules():
    assert kwh_to_joules("1") == 3_600_000
    assert kwh_to_joules(1) == 3_600_000
    assert kwh_to_joules(Decimal(1)) == 3_600_000


def test_one_mwh_is_exactly_3_6_gigajoules():
    assert mwh_to_joules("1") == 3_600_000_000
    assert mwh_to_joules("1") == Decimal("3.6") * JOULES_PER_GJ


def test_five_decimal_places_of_kwh_are_exact_and_six_are_not():
    """Where exactness actually stops, pinned by value on both sides of the line.

    This test is the reason units.py says five rather than six. The module's
    docstring claimed six, which is the answer plausible reasoning produces, and
    this assertion is what refuted it: 10**-5 kWh is 36J on the nose, while
    10**-6 kWh is 3.6J and lands nowhere.

    Both sides are asserted deliberately. A test that only pinned the exact side
    would still pass with EXACT_KWH_PLACES set to any wrong larger value, so it
    would not have caught the defect it was written for.
    """
    # Exact: every value at or inside five places lands on a whole joule.
    assert kwh_to_joules("0.00001") == 36
    assert kwh_to_joules("0.001") == 3_600
    assert kwh_to_joules("1234.567") == 1234567 * 3_600
    assert kwh_to_joules("0.12345") == Decimal("0.12345") * JOULES_PER_KWH

    # Inexact: the sixth place is 3.6J and has to round. Half-to-even, and the
    # residue is visible rather than swallowed.
    assert Decimal("0.123456") * JOULES_PER_KWH == Decimal("444441.6")
    assert kwh_to_joules("0.123456") == 444_442
    assert kwh_to_joules("0.000001") == 4

    # And the constant says the same thing the assertions above do.
    assert EXACT_KWH_PLACES == 5
    assert Decimal(10) ** -EXACT_KWH_PLACES * JOULES_PER_KWH == 36


def test_float_is_refused_rather_than_rounded():
    """The refusal that stops an inexact input being laundered into an exact one."""
    with pytest.raises(EnergyValueError, match="refuses float"):
        kwh_to_joules(1.0)
    with pytest.raises(EnergyValueError, match="refuses float"):
        mwh_to_joules(0.5)


def test_negative_energy_is_refused():
    with pytest.raises(EnergyValueError, match="negative energy"):
        kwh_to_joules("-1")


def test_non_numeric_is_refused_not_silently_zero():
    with pytest.raises(EnergyValueError):
        kwh_to_joules("not a number")
    with pytest.raises(EnergyValueError):
        kwh_to_joules("NaN")
    with pytest.raises(EnergyValueError):
        kwh_to_joules("Infinity")


def test_bool_is_not_an_energy_amount():
    """bool subclasses int in Python, so this would render as 1J without the guard."""
    with pytest.raises(EnergyValueError):
        format_gj(True)


def test_display_has_no_space_before_the_unit():
    """One quantity reads as one token, and a space breaks column alignment."""
    assert format_gj(JOULES_PER_GJ) == "1.000000GJ"
    assert " GJ" not in format_gj(3_600_000)
    assert format_gj(3_600_000) == "0.003600GJ"


def test_round_trip_through_gigajoules_is_exact():
    joules = 12_345_678_901
    assert joules_to_gj(joules) * JOULES_PER_GJ == joules
