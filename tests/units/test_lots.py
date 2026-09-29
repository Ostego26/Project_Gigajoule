"""Lot selection: which joules leave when somebody is paid.

Role: test (lot selection)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

Every case here is seeded values against a pure function. No database, because
the decision does not need one -- which is the argument for having put the
decision in its own module instead of inside the SQL that reads holdings.
"""

from __future__ import annotations

import pytest

from gigajoule_core.lots import (
    ANY_LOT,
    Holding,
    InsufficientHoldings,
    LotSpec,
    eligible,
    select_lots,
)

HOUR = 3600
T0 = 1_800_000_000


def lot(name: str, joules: int, *, hour: int = 0, fuel: str = "solar", region: str = "ERCOT"):
    start = T0 + hour * HOUR
    return Holding(
        lot_id=name,
        joules=joules,
        vintage_start=start,
        vintage_end=start + HOUR,
        fuel=fuel,
        grid_region=region,
    )


def test_selection_sums_to_exactly_the_amount_requested():
    """Exact or refuse. Never close enough, in either direction."""
    held = [lot("a", 100, hour=0), lot("b", 100, hour=1)]
    picked = select_lots(held, 150)
    assert sum(joules for _, joules in picked) == 150
    assert picked == [("a", 100), ("b", 50)]


def test_oldest_vintage_is_spent_first():
    """The default rule, pinned by order rather than by reading the docstring."""
    held = [lot("new", 100, hour=5), lot("old", 100, hour=0), lot("mid", 100, hour=2)]
    assert [lot_id for lot_id, _ in select_lots(held, 250)] == ["old", "mid", "new"]


def test_ties_on_vintage_break_stably_on_lot_id():
    """An unstable tie break is a reproducibility bug that only shows up later.

    Hourly settlement means most lots share a vintage, so this is the common case
    rather than the edge one: two auditors replaying the same ledger have to
    reach the same answer.
    """
    held = [lot("zzz", 50, hour=0), lot("aaa", 50, hour=0), lot("mmm", 50, hour=0)]
    assert [lot_id for lot_id, _ in select_lots(held, 150)] == ["aaa", "mmm", "zzz"]
    # And the order does not depend on the order the holdings arrived in.
    assert select_lots(list(reversed(held)), 150) == select_lots(held, 150)


def test_a_lot_is_only_split_when_it_has_to_be():
    held = [lot("a", 100, hour=0), lot("b", 100, hour=1)]
    assert select_lots(held, 100) == [("a", 100)]
    assert select_lots(held, 1) == [("a", 1)]


def test_a_spec_is_a_filter_and_never_a_preference():
    """Asking for solar and being given wind is the failure this prevents.

    The attribute IS the product, so a partial match is not a match. If the only
    eligible lots cannot cover the amount, the answer is a refusal even though
    the account holds plenty of joules.
    """
    held = [lot("wind", 1000, hour=0, fuel="wind"), lot("solar", 100, hour=1)]
    assert select_lots(held, 100, LotSpec(fuel="solar")) == [("solar", 100)]
    with pytest.raises(InsufficientHoldings) as exc:
        select_lots(held, 500, LotSpec(fuel="solar"))
    assert "SPECIFICATION" in str(exc.value), (
        "the refusal must say the shortfall was the spec and not the amount -- "
        "those want opposite fixes"
    )


def test_the_shortfall_message_distinguishes_amount_from_specification():
    held = [lot("a", 100, hour=0)]
    with pytest.raises(InsufficientHoldings) as exc:
        select_lots(held, 500)
    message = str(exc.value)
    assert "Short by 400J" in message
    assert "SPECIFICATION" not in message, "here the amount really was the problem"


def test_a_vintage_window_requires_the_lot_to_sit_entirely_inside_it():
    """Overlap would be the looser reading, and it is wrong.

    A lot straddling the boundary carries energy generated outside the window the
    buyer asked for, and a lot cannot be split by provenance -- that is what
    makes it a lot.
    """
    held = [lot("inside", 100, hour=2), lot("straddling", 100, hour=0)]
    window = LotSpec(vintage_start=T0 + 2 * HOUR, vintage_end=T0 + 4 * HOUR)
    assert [h.lot_id for h in eligible(held, window)] == ["inside"]


def test_region_and_fuel_filter_independently():
    held = [
        lot("a", 100, hour=0, fuel="solar", region="ERCOT"),
        lot("b", 100, hour=1, fuel="solar", region="CAISO"),
        lot("c", 100, hour=2, fuel="wind", region="ERCOT"),
    ]
    assert [h.lot_id for h in eligible(held, LotSpec(grid_region="ERCOT"))] == ["a", "c"]
    assert [h.lot_id for h in eligible(held, LotSpec(fuel="solar"))] == ["a", "b"]
    assert [
        h.lot_id for h in eligible(held, LotSpec(fuel="solar", grid_region="CAISO"))
    ] == ["b"]


def test_an_empty_or_negative_request_is_refused():
    with pytest.raises(ValueError, match="positive"):
        select_lots([lot("a", 100)], 0)
    with pytest.raises(ValueError, match="positive"):
        select_lots([lot("a", 100)], -5)


def test_holding_nothing_refuses_rather_than_returning_empty():
    """An empty list would read as "paid successfully with no lots"."""
    with pytest.raises(InsufficientHoldings):
        select_lots([], 1, ANY_LOT)
