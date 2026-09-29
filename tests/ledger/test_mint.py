"""Minting: one interval mints once, and only if it was physically possible.

Role: test (minting and refusal)
Reads: a temporary ledger database
Writes: a temporary ledger database
Can move tokens: no
Live-safe: yes

The two defects these cover are the ones that make an energy token worthless
rather than merely wrong. Minting the same interval twice sells the same joule
to two buyers. Minting an impossible reading backs a token with energy that was
never generated. Everything else in this system is arrangement; these two are
the product.
"""

from __future__ import annotations

import sqlite3

import pytest

from gigajoule_core import ledger
from gigajoule_core.units import kwh_to_joules

SEED_NOW = 1_800_000_000
HOUR = 3600
ONE_KWH = 3_600_000


def reading(reading_id: str, joules: int, *, hours: int = 1, offset: int = 0) -> ledger.MeterReading:
    """One hour of generation from the seeded 1kW source, unless told otherwise."""
    start = SEED_NOW + offset * HOUR
    return ledger.MeterReading(
        reading_id=reading_id,
        source_id="solar-01",
        interval_start=start,
        interval_end=start + hours * HOUR,
        joules=joules,
        attestor="meter-co",
    )


def test_one_interval_mints_exactly_once(seeded: sqlite3.Connection):
    """The second attempt is refused by ledger.py AND by a unique index."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH), now=SEED_NOW)
    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)

    with pytest.raises(ledger.LedgerError, match="already minted"):
        ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)

    assert ledger.balance(seeded, "acme") == ONE_KWH


def test_the_database_refuses_a_second_mint_even_without_ledger_py(seeded: sqlite3.Connection):
    """Raw SQL, because the index is the rule and ledger.py is only the early warning."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH), now=SEED_NOW)
    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)

    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO gj_ledger_entry "
            "(batch_id, account_id, joules_delta, reason, reading_id, memo, recorded_at) "
            "VALUES ('sneaky', 'acme', 1, 'MINT', 'r1', '', ?)",
            (SEED_NOW,),
        )
    assert ledger.balance(seeded, "acme") == ONE_KWH


def test_the_same_interval_cannot_be_submitted_twice(seeded: sqlite3.Connection):
    """A resubmitted meter file is the ordinary case, and it fails at insert."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH), now=SEED_NOW)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_reading(seeded, reading("r2-different-id", ONE_KWH), now=SEED_NOW)


def test_a_physically_impossible_reading_is_kept_but_cannot_mint(seeded: sqlite3.Connection):
    """The Wh/kWh unit error: 1 MWh claimed from a 1kW source in one hour.

    Every part of the outcome is asserted, because the design is that all four
    are true at once -- the evidence survives, the refusal is recorded with its
    reason, the queue stays clean, and no token exists.
    """
    accepted = ledger.record_reading(seeded, reading("bad", kwh_to_joules("1000")), now=SEED_NOW)
    assert accepted is False

    kept = seeded.execute(
        "SELECT joules FROM gj_meter_reading WHERE reading_id = 'bad'"
    ).fetchone()
    assert kept["joules"] == 3_600_000_000, "the reading itself must survive as evidence"

    refusal = seeded.execute("SELECT * FROM v_gj_refused_reading").fetchone()
    assert refusal["check_name"] == "physical_ceiling"
    assert "1000" in refusal["detail"], "the ratio names the unit error for the operator"

    queued = seeded.execute("SELECT COUNT(*) AS n FROM v_gj_unminted_reading").fetchone()["n"]
    assert queued == 0, "a broken input must not look like a backlog item"

    with pytest.raises(ledger.LedgerError, match="refused at record time"):
        ledger.mint(seeded, "bad", to_account="acme", now=SEED_NOW)
    assert ledger.balance(seeded, "acme") == 0


def test_a_reading_at_the_ceiling_is_allowed(seeded: sqlite3.Connection):
    """1kW for one hour is exactly 1kWh, and must not be refused by an off-by-one."""
    assert ledger.record_reading(seeded, reading("exact", ONE_KWH), now=SEED_NOW) is True
    ledger.mint(seeded, "exact", to_account="acme", now=SEED_NOW)
    assert ledger.balance(seeded, "acme") == ONE_KWH


def test_modest_overshoot_within_tolerance_is_allowed(seeded: sqlite3.Connection):
    """Inverter clipping and gusts put a real source slightly over nameplate."""
    assert ledger.record_reading(seeded, reading("clip", ONE_KWH + 100_000), now=SEED_NOW) is True


def test_mint_pending_reports_what_it_did_and_what_it_skipped(seeded: sqlite3.Connection):
    """"Did nothing" and "did work" must not read the same on a status line."""
    ledger.record_reading(seeded, reading("ok1", ONE_KWH, offset=0), now=SEED_NOW)
    ledger.record_reading(seeded, reading("ok2", ONE_KWH, offset=1), now=SEED_NOW)
    ledger.record_reading(seeded, reading("zero", 0, offset=2), now=SEED_NOW)
    ledger.record_reading(seeded, reading("huge", kwh_to_joules("9999"), offset=3), now=SEED_NOW)

    outcome = ledger.mint_pending(seeded, to_account="acme", now=SEED_NOW)

    assert outcome.minted == 2
    assert outcome.joules == 2 * ONE_KWH
    assert len(outcome.skipped) == 1, "the refused reading was never queued; the 0J one was"
    assert "zero" in outcome.skipped[0]
    assert ledger.balance(seeded, "acme") == 2 * ONE_KWH

    again = ledger.mint_pending(seeded, to_account="acme", now=SEED_NOW)
    assert again.minted == 0, "a second pass must not re-mint anything"


def test_a_mint_can_be_reversed_and_the_reading_still_cannot_re_mint(seeded: sqlite3.Connection):
    """A reversal adds history; it does not make the interval available again."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH), now=SEED_NOW)
    batch_id = ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)
    assert ledger.balance(seeded, "acme") == ONE_KWH

    ledger.reverse(seeded, batch_id, now=SEED_NOW, memo="wrong holder")
    assert ledger.balance(seeded, "acme") == 0
    assert ledger.conservation(seeded)["residual_joules"] == 0

    with pytest.raises(ledger.LedgerError, match="already minted"):
        ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)

    entries = seeded.execute("SELECT COUNT(*) AS n FROM gj_ledger_entry").fetchone()["n"]
    assert entries == 4, "both the mint and its reversal stay visible"


def test_a_retirement_cannot_be_reversed(seeded: sqlite3.Connection):
    """Un-claiming would return tokens to circulation against energy already spent."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH), now=SEED_NOW)
    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)
    batch_id = ledger.retire(
        seeded, ledger.Claim(account_id="acme", joules=ONE_KWH), now=SEED_NOW
    )

    with pytest.raises(ledger.LedgerError, match="cannot be reversed"):
        ledger.reverse(seeded, batch_id, now=SEED_NOW, memo="changed our mind")
    assert ledger.balance(seeded, "acme") == 0


def test_a_reading_from_an_unregistered_source_is_refused(ledger_db: sqlite3.Connection):
    """Every token traces to a source whose nameplate and fuel are on record."""
    with pytest.raises(ledger.LedgerError, match="unknown source"):
        ledger.record_reading(ledger_db, reading("orphan", ONE_KWH), now=SEED_NOW)


def test_provenance_traces_every_minted_joule_to_its_source(seeded: sqlite3.Connection):
    """What a buyer is actually paying for, as a view rather than a report."""
    ledger.record_reading(seeded, reading("r1", ONE_KWH, offset=0), now=SEED_NOW)
    ledger.record_reading(seeded, reading("r2", ONE_KWH, offset=1), now=SEED_NOW)
    ledger.mint_pending(seeded, to_account="acme", now=SEED_NOW)

    rows = seeded.execute("SELECT * FROM v_gj_provenance").fetchall()
    assert len(rows) == 1
    assert rows[0]["account_id"] == "acme"
    assert rows[0]["fuel"] == "solar"
    assert rows[0]["grid_region"] == "ERCOT"
    assert rows[0]["origin"] == "GENERATION"
    assert rows[0]["joules"] == 2 * ONE_KWH
    # Two readings, two lots: a lot is one parcel of one provenance, and merging
    # two intervals into one would throw away the vintage distinction that every
    # time-matched claim rests on.
    assert rows[0]["lots"] == 2
