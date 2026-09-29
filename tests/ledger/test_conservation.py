"""Conservation: joules minted equal joules held plus joules retired, exactly.

Role: test (ledger invariants)
Reads: a temporary ledger database
Writes: a temporary ledger database
Can move tokens: no
Live-safe: yes

THE INVARIANT IS CHECKED AFTER EVERY SINGLE OPERATION, not once at the end. A
check that only runs at the end of a lifecycle tells you that something broke
somewhere in it; a check after each step tells you which step. The cost is one
indexed SUM per assertion against a database holding single-digit rows, which is
nothing, and the diagnostic difference is the whole of an investigation.
"""

from __future__ import annotations

import sqlite3

import pytest

from gigajoule_core import ledger
from gigajoule_core.schema import ISSUANCE_ACCOUNT, RETIREMENT_ACCOUNT

SEED_NOW = 1_800_000_000
HOUR = 3600
ONE_KWH = 3_600_000


def assert_conserved(conn: sqlite3.Connection) -> sqlite3.Row:
    """Every ledger invariant that can be read from a view, in one call."""
    row = ledger.conservation(conn)
    assert row["residual_joules"] == 0, (
        f"conservation broken by {row['residual_joules']}J -- the ledger has "
        "created or destroyed energy, which no operation is allowed to do"
    )
    # From the view, not recomputed. A test that does its own arithmetic is a
    # second copy of the rule, and a second copy is what let the CLI drift.
    assert row["supply_residual_joules"] == 0, "the supply identity does not close"
    imbalanced = conn.execute("SELECT * FROM v_gj_imbalanced_batch").fetchall()
    assert not imbalanced, f"batches whose legs do not sum to zero: {[dict(r) for r in imbalanced]}"

    # Per-lot conservation. Catches what the global sum cannot: a batch that
    # debits one lot and credits another nets to zero overall while having moved
    # energy between provenances with no record of the transformation.
    stray = conn.execute("SELECT * FROM v_gj_imbalanced_lot").fetchall()
    assert not stray, f"lots whose entries do not sum to zero: {[dict(r) for r in stray]}"

    # CONVERSION is a bridge and nets out inside each batch. A non-zero total
    # means a discharge wrote one side of it.
    assert row["conversion_joules"] == 0, "CONVERSION holds a position; a lot bridge is half-written"
    return row


def test_a_full_lifecycle_conserves_at_every_step(seeded: sqlite3.Connection):
    """Register, measure, mint, transfer, retire -- residual 0 after each."""
    assert_conserved(seeded)
    ledger.open_account(seeded, "buyer", "Buyer Ltd", now=SEED_NOW)

    ledger.record_reading(
        seeded,
        ledger.MeterReading(
            reading_id="r1",
            source_id="solar-01",
            interval_start=SEED_NOW,
            interval_end=SEED_NOW + HOUR,
            joules=ONE_KWH,
            attestor="meter-co",
        ),
        now=SEED_NOW,
    )
    assert_conserved(seeded)

    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)
    row = assert_conserved(seeded)
    assert row["minted_joules"] == ONE_KWH
    assert row["circulating_joules"] == ONE_KWH
    assert row["retired_joules"] == 0

    ledger.transfer(
        seeded,
        ledger.Movement(from_account="acme", to_account="buyer", joules=ONE_KWH // 2),
        now=SEED_NOW,
    )
    row = assert_conserved(seeded)
    assert row["minted_joules"] == ONE_KWH, "a transfer must not change the minted total"
    assert ledger.balance(seeded, "acme") == ONE_KWH // 2
    assert ledger.balance(seeded, "buyer") == ONE_KWH // 2

    ledger.retire(
        seeded,
        ledger.Claim(account_id="buyer", joules=ONE_KWH // 2, memo="Q3 claim"),
        now=SEED_NOW,
    )
    row = assert_conserved(seeded)
    assert row["retired_joules"] == ONE_KWH // 2
    assert row["circulating_joules"] == ONE_KWH // 2
    assert ledger.balance(seeded, "buyer") == 0
    assert ledger.balance(seeded, RETIREMENT_ACCOUNT) == ONE_KWH // 2
    assert ledger.balance(seeded, ISSUANCE_ACCOUNT) == -ONE_KWH


def test_an_unbalanced_batch_is_refused_before_it_is_written(seeded: sqlite3.Connection):
    """The cheap check that keeps v_gj_imbalanced_batch a proof rather than a detector."""
    # The lot id is a fake: `_post_batch` checks the arithmetic BEFORE opening a
    # transaction, so this is refused without ever reaching the foreign key --
    # which is itself the behavior being pinned.
    batch = ledger.Batch(legs=[ledger.Leg("acme", 100, "no-such-lot")], reason="TRANSFER")
    assert batch.residual() == 100
    with pytest.raises(ledger.LedgerError, match="unbalanced batch"):
        ledger._post_batch(seeded, batch, now=SEED_NOW)
    assert_conserved(seeded)
    assert seeded.execute("SELECT COUNT(*) AS n FROM gj_ledger_entry").fetchone()["n"] == 0


def test_an_empty_batch_is_refused(seeded: sqlite3.Connection):
    with pytest.raises(ledger.LedgerError, match="empty batch"):
        ledger._post_batch(seeded, ledger.Batch(legs=[], reason="TRANSFER"), now=SEED_NOW)


def test_a_self_transfer_is_refused(seeded: sqlite3.Connection):
    """Two legs that change nothing are a puzzle for every later reader."""
    with pytest.raises(ledger.LedgerError, match="self-transfer"):
        ledger.transfer(
            seeded,
            ledger.Movement(from_account="acme", to_account="acme", joules=1),
            now=SEED_NOW,
        )


def test_conservation_holds_across_many_operations(seeded: sqlite3.Connection):
    """Fifty intervals through the full lifecycle, one database, exact at the end.

    Fifty rather than fifty databases: the claim is about the ledger's arithmetic
    over many operations, and seeding them into one database proves it at the
    same strength for a fraction of the wall clock. The scaffolding is where the
    cost of a thorough test usually lives, not in the thing being proven.
    """
    ledger.open_account(seeded, "buyer", "Buyer Ltd", now=SEED_NOW)
    total = 0
    for i in range(50):
        joules = ONE_KWH + i
        start = SEED_NOW + i * HOUR
        ledger.record_reading(
            seeded,
            ledger.MeterReading(
                reading_id=f"r{i}",
                source_id="solar-01",
                interval_start=start,
                interval_end=start + HOUR,
                joules=joules,
                attestor="meter-co",
            ),
            now=SEED_NOW,
        )
        ledger.mint(seeded, f"r{i}", to_account="acme", now=SEED_NOW)
        total += joules
        if i % 3 == 0:
            ledger.transfer(
                seeded,
                ledger.Movement(from_account="acme", to_account="buyer", joules=1_000),
                now=SEED_NOW,
            )
        if i % 5 == 0:
            ledger.retire(seeded, ledger.Claim(account_id="acme", joules=500), now=SEED_NOW)
        assert_conserved(seeded)

    row = assert_conserved(seeded)
    assert row["minted_joules"] == total
    assert row["retired_joules"] == 500 * 10
    assert row["circulating_joules"] == total - 5_000
