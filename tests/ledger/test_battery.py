"""Storage: energy moved in time, at a cost, without laundering its provenance.

Role: test (storage behavior)
Reads: a temporary ledger database
Writes: a temporary ledger database
Can move tokens: no
Live-safe: yes

Discharge is the only operation in this system that creates a lot without a
meter reading, so it is the one place energy could appear from nowhere or change
what it claims to be. Every test here is aimed at one of those two.
"""

from __future__ import annotations

import sqlite3

import pytest

from gigajoule_core import ledger
from gigajoule_core.lots import InsufficientHoldings
from gigajoule_core.schema import CONVERSION_ACCOUNT, LOSS_ACCOUNT

SEED_NOW = 1_800_000_000
HOUR = 3600
ONE_KWH = 3_600_000
RELEASE_AT = SEED_NOW + 12 * HOUR


def assert_conserved(conn: sqlite3.Connection) -> sqlite3.Row:
    """Every invariant, after every operation. Cheap, and it names the step."""
    row = ledger.conservation(conn)
    assert row["residual_joules"] == 0
    # From the view, not recomputed. A test that does its own arithmetic is a
    # second copy of the rule, and a second copy is what let the CLI drift.
    assert row["supply_residual_joules"] == 0, "the supply identity does not close"
    assert row["conversion_joules"] == 0, "CONVERSION holds a position; a lot bridge is half-written"
    assert not conn.execute("SELECT * FROM v_gj_imbalanced_lot").fetchall()
    assert not conn.execute("SELECT * FROM v_gj_imbalanced_batch").fetchall()
    return row


def stock(conn: sqlite3.Connection, *, fuel: str, source_id: str, hour: int, reading_id: str):
    """Register a source if needed, meter an hour of it, and mint it to acme."""
    existing = conn.execute(
        "SELECT 1 FROM gj_source WHERE source_id = ?", (source_id,)
    ).fetchone()
    if existing is None:
        ledger.register_source(
            conn,
            ledger.Source(
                source_id=source_id, fuel=fuel, grid_region="ERCOT", nameplate_watts=1_000
            ),
            now=SEED_NOW,
        )
    ledger.record_reading(
        conn,
        ledger.MeterReading(
            reading_id=reading_id,
            source_id=source_id,
            interval_start=SEED_NOW + hour * HOUR,
            interval_end=SEED_NOW + (hour + 1) * HOUR,
            joules=ONE_KWH,
            attestor="meter-co",
        ),
        now=SEED_NOW,
    )
    ledger.mint(conn, reading_id, to_account="acme", now=SEED_NOW)


@pytest.fixture
def battery(seeded: sqlite3.Connection) -> sqlite3.Connection:
    """A 10kWh battery at 88% round trip, and a recipient to discharge into."""
    ledger.register_storage_asset(
        seeded,
        ledger.StorageAsset(
            asset_id="bess-01",
            account_id="storage:bess-01",
            grid_region="ERCOT",
            capacity_joules=10 * ONE_KWH,
            round_trip_bp=8_800,
        ),
        now=SEED_NOW,
    )
    ledger.open_account(seeded, "grid", "Grid Offtaker", now=SEED_NOW)
    return seeded


def test_charging_moves_title_without_touching_provenance(battery: sqlite3.Connection):
    """Nothing has happened to the energy yet, so nothing happens to its lot."""
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="r0")
    before = battery.execute("SELECT lot_id, fuel, vintage_start FROM gj_lot").fetchall()

    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=ONE_KWH),
        now=SEED_NOW,
    )
    after = battery.execute("SELECT lot_id, fuel, vintage_start FROM gj_lot").fetchall()

    assert [dict(r) for r in before] == [dict(r) for r in after], "charging created or changed a lot"
    assert ledger.balance(battery, "storage:bess-01") == ONE_KWH
    assert ledger.balance(battery, "acme") == 0
    assert assert_conserved(battery)["stored_joules"] == ONE_KWH


def test_discharge_loses_energy_and_the_ledger_says_where_it_went(battery: sqlite3.Connection):
    """100 in, 88 out, 12 to LOSS -- and conservation still exact."""
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="r0")
    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=ONE_KWH),
        now=SEED_NOW,
    )

    outcome = ledger.discharge(
        battery,
        ledger.Discharge(asset_id="bess-01", to_account="grid", joules=ONE_KWH),
        now=RELEASE_AT,
    )

    assert outcome.drawn_joules == ONE_KWH
    assert outcome.released_joules == 3_168_000       # 88% of 1kWh
    assert outcome.lost_joules == 432_000
    assert outcome.released_joules + outcome.lost_joules == outcome.drawn_joules

    assert ledger.balance(battery, "grid") == 3_168_000
    assert ledger.balance(battery, LOSS_ACCOUNT) == 432_000
    assert ledger.balance(battery, "storage:bess-01") == 0
    assert ledger.balance(battery, CONVERSION_ACCOUNT) == 0, "the bridge must net to zero"

    row = assert_conserved(battery)
    assert row["minted_joules"] == ONE_KWH, "a discharge must not mint anything"
    assert row["lost_joules"] == 432_000


def test_the_released_lot_carries_the_release_time_not_the_generation_time(
    battery: sqlite3.Connection,
):
    """THE WHOLE REASON A BATTERY IS INTERESTING HERE.

    Energy generated at midnight and delivered twelve hours later is a different
    product. A child lot inheriting its parent's vintage would let a battery
    launder off-peak generation into peak delivery, which is exactly the
    time-shifting misstatement 24/7 carbon-free accounting exists to prevent.
    """
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="r0")
    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=ONE_KWH),
        now=SEED_NOW,
    )
    outcome = ledger.discharge(
        battery,
        ledger.Discharge(asset_id="bess-01", to_account="grid", joules=ONE_KWH),
        now=RELEASE_AT,
    )

    child = battery.execute(
        "SELECT * FROM gj_lot WHERE lot_id = ?", (outcome.child_lots[0],)
    ).fetchone()
    assert child["origin"] == "STORAGE_RELEASE"
    assert child["vintage_start"] == RELEASE_AT, "the child must be stamped at RELEASE time"
    assert child["vintage_start"] != SEED_NOW, "it must not inherit the generation window"
    assert child["fuel"] == "solar", "solar in, solar out -- storage moves time, not fuel"

    lineage = battery.execute("SELECT * FROM v_gj_lot_lineage").fetchone()
    assert lineage["generated_at"] == SEED_NOW
    assert lineage["released_at"] == RELEASE_AT
    assert lineage["parent_reading_id"] == "r0"


def test_a_mixed_battery_releases_one_lot_per_fuel_and_launders_nothing(
    battery: sqlite3.Connection,
):
    """The anti-laundering case, and the reason discharge does not merge lots.

    A battery holding solar and wind must release some of each, with each child
    naming its own parent. A single merged child lot would have to pick one fuel
    and be wrong about the rest -- which would make a battery a machine for
    turning wind into solar.
    """
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="solar-r")
    stock(battery, fuel="wind", source_id="wind-01", hour=1, reading_id="wind-r")
    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=2 * ONE_KWH),
        now=SEED_NOW,
    )

    outcome = ledger.discharge(
        battery,
        ledger.Discharge(asset_id="bess-01", to_account="grid", joules=2 * ONE_KWH),
        now=RELEASE_AT,
    )
    assert len(outcome.child_lots) == 2, "one child lot per parent lot, never a merge"

    delivered = battery.execute(
        "SELECT fuel, SUM(joules) AS joules FROM v_gj_holding "
        " WHERE account_id = 'grid' GROUP BY fuel ORDER BY fuel"
    ).fetchall()
    assert [(r["fuel"], r["joules"]) for r in delivered] == [
        ("solar", 3_168_000),
        ("wind", 3_168_000),
    ]
    assert_conserved(battery)


def test_a_battery_cannot_hold_more_than_its_capacity(battery: sqlite3.Connection):
    """Enforced by a trigger, so it holds for a caller that never imports ledger.py."""
    for hour in range(11):
        stock(battery, fuel="solar", source_id="solar-01", hour=hour, reading_id=f"r{hour}")
    assert ledger.balance(battery, "acme") == 11 * ONE_KWH

    with pytest.raises(sqlite3.IntegrityError, match="capacity"):
        ledger.charge(
            battery,
            ledger.Charge(asset_id="bess-01", from_account="acme", joules=11 * ONE_KWH),
            now=SEED_NOW,
        )
    assert ledger.balance(battery, "storage:bess-01") == 0, "the refused charge left nothing behind"
    assert_conserved(battery)


def test_a_battery_cannot_discharge_what_it_never_stored(battery: sqlite3.Connection):
    with pytest.raises(InsufficientHoldings):
        ledger.discharge(
            battery,
            ledger.Discharge(asset_id="bess-01", to_account="grid", joules=ONE_KWH),
            now=RELEASE_AT,
        )
    assert_conserved(battery)


def test_a_discharge_cannot_be_reversed(battery: sqlite3.Connection):
    """The loss is not a bookkeeping entry. It left as heat."""
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="r0")
    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=ONE_KWH),
        now=SEED_NOW,
    )
    outcome = ledger.discharge(
        battery,
        ledger.Discharge(asset_id="bess-01", to_account="grid", joules=ONE_KWH),
        now=RELEASE_AT,
    )
    with pytest.raises(ledger.LedgerError, match="cannot be reversed"):
        ledger.reverse(battery, outcome.batch_id, now=RELEASE_AT, memo="oops")
    assert ledger.balance(battery, "grid") == 3_168_000


def test_storage_state_reports_headroom_an_operator_can_act_on(battery: sqlite3.Connection):
    stock(battery, fuel="solar", source_id="solar-01", hour=0, reading_id="r0")
    ledger.charge(
        battery,
        ledger.Charge(asset_id="bess-01", from_account="acme", joules=ONE_KWH),
        now=SEED_NOW,
    )
    state = battery.execute("SELECT * FROM v_gj_storage_state").fetchone()
    assert state["charged_joules"] == ONE_KWH
    assert state["headroom_joules"] == 9 * ONE_KWH
    assert state["capacity_joules"] == 10 * ONE_KWH
    assert state["lots_held"] == 1
