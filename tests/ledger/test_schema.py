"""The rules the DATABASE enforces, tested by bypassing the Python that also does.

Role: test (schema)
Reads: a temporary ledger database
Writes: a temporary ledger database
Can move tokens: no
Live-safe: yes

EVERY TEST HERE WRITES RAW SQL ON PURPOSE. `ledger.py` refuses these operations
too, and a test that went through `ledger.py` would pass just as well with every
trigger and constraint dropped from the schema -- it would be proving the Python
guard and reporting it as proof of the database's. The point of putting the
rules in SQL is that they hold for a caller who never imports this package, so
the tests for them have to be that caller.
"""

from __future__ import annotations

import sqlite3

import pytest

from gigajoule_core import ledger
from gigajoule_core.db import connect
from gigajoule_core.schema import ACCOUNT_KINDS, ISSUANCE_ACCOUNT, LEDGER_REASONS

SEED_NOW = 1_800_000_000


def test_vocabulary_tables_match_the_python_constants(ledger_db: sqlite3.Connection):
    """The derivation check that stops the two copies of each vocabulary drifting.

    `ACCOUNT_KINDS` and `LEDGER_REASONS` exist in Python for callers to import;
    the same values exist as rows because a foreign key enforces what a CHECK
    list built from an f-string cannot. Two places, so one assertion holds them
    together -- and it fails from either side, whichever one somebody edits.
    """
    kinds = {r["kind"] for r in ledger_db.execute("SELECT kind FROM gj_account_kind")}
    reasons = {r["reason"] for r in ledger_db.execute("SELECT reason FROM gj_ledger_reason")}
    assert kinds == set(ACCOUNT_KINDS)
    assert reasons == set(LEDGER_REASONS)


def test_every_vocabulary_row_explains_itself(ledger_db: sqlite3.Connection):
    """A vocabulary table earns its place over a CHECK list by carrying meaning.

    The two queries are written out rather than built in a loop over table names.
    The loop was the first draft and it needed an f-string, which needed a `noqa`
    on a repo whose standing rule is that a suppression is never how a check is
    made to pass -- for four lines of saved typing, in a test, where the whole
    value is that a reader can see exactly what was asked of the database.
    """
    kinds = ledger_db.execute("SELECT kind AS value, description FROM gj_account_kind").fetchall()
    reasons = ledger_db.execute(
        "SELECT reason AS value, description FROM gj_ledger_reason"
    ).fetchall()
    assert kinds and reasons
    for row in [*kinds, *reasons]:
        assert len(row["description"]) > 40, f"{row['value']} has no real description"


def test_connect_turns_foreign_keys_on(ledger_db: sqlite3.Connection):
    """SQLite defaults this OFF, per connection, and it fails by ACCEPTING rows.

    Asserted behaviorally as well as by pragma: a reference to an account that
    does not exist must be refused, which is the thing the pragma buys.
    """
    assert ledger_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        ledger_db.execute(
            "INSERT INTO gj_ledger_entry "
            "(batch_id, account_id, joules_delta, reason, reading_id, memo, recorded_at) "
            "VALUES ('b', 'no-such-account', 1, 'TRANSFER', NULL, '', ?)",
            (SEED_NOW,),
        )


def test_the_ledger_cannot_be_edited_or_deleted(seeded: sqlite3.Connection):
    """Append-only, enforced by the database rather than by review."""
    ledger.record_reading(
        seeded,
        ledger.MeterReading(
            reading_id="r1",
            source_id="solar-01",
            interval_start=SEED_NOW,
            interval_end=SEED_NOW + 3600,
            joules=3_600_000,
            attestor="meter-co",
        ),
        now=SEED_NOW,
    )
    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)
    before = ledger.balance(seeded, "acme")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        seeded.execute("UPDATE gj_ledger_entry SET joules_delta = 999 WHERE account_id = 'acme'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        seeded.execute("DELETE FROM gj_ledger_entry WHERE account_id = 'acme'")

    assert ledger.balance(seeded, "acme") == before


def test_a_reading_cannot_be_restated(seeded: sqlite3.Connection):
    """Evidence of what was measured is never edited or removed."""
    ledger.record_reading(
        seeded,
        ledger.MeterReading(
            reading_id="r1",
            source_id="solar-01",
            interval_start=SEED_NOW,
            interval_end=SEED_NOW + 3600,
            joules=3_600_000,
            attestor="meter-co",
        ),
        now=SEED_NOW,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        seeded.execute("UPDATE gj_meter_reading SET joules = 1 WHERE reading_id = 'r1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        seeded.execute("DELETE FROM gj_meter_reading WHERE reading_id = 'r1'")


def test_a_holder_cannot_spend_what_it_does_not_hold(seeded: sqlite3.Connection):
    """The overdraft trigger, reached by raw SQL so it is the trigger being tested."""
    with pytest.raises(sqlite3.IntegrityError, match="overdraft"):
        seeded.execute(
            "INSERT INTO gj_ledger_entry "
            "(batch_id, account_id, joules_delta, reason, reading_id, memo, recorded_at) "
            "VALUES ('b', 'acme', -1, 'TRANSFER', NULL, '', ?)",
            (SEED_NOW,),
        )
    assert ledger.balance(seeded, "acme") == 0


def test_issuance_is_allowed_to_go_negative(seeded: sqlite3.Connection):
    """The exemption the overdraft rule needs, since issuance IS the minted total."""
    ledger.record_reading(
        seeded,
        ledger.MeterReading(
            reading_id="r1",
            source_id="solar-01",
            interval_start=SEED_NOW,
            interval_end=SEED_NOW + 3600,
            joules=3_600_000,
            attestor="meter-co",
        ),
        now=SEED_NOW,
    )
    ledger.mint(seeded, "r1", to_account="acme", now=SEED_NOW)
    assert ledger.balance(seeded, ISSUANCE_ACCOUNT) == -3_600_000


def test_there_can_only_be_one_issuance_account(ledger_db: sqlite3.Connection):
    """A second would split the minted total and every supply figure would understate."""
    with pytest.raises(sqlite3.IntegrityError):
        ledger_db.execute(
            "INSERT INTO gj_account (account_id, kind, display_name, opened_at) "
            "VALUES ('rogue', 'ISSUANCE', 'Second issuance', ?)",
            (SEED_NOW,),
        )


def test_an_unknown_account_kind_is_refused(ledger_db: sqlite3.Connection):
    """The foreign key onto gj_account_kind, doing what the CHECK list used to."""
    with pytest.raises(sqlite3.IntegrityError):
        ledger_db.execute(
            "INSERT INTO gj_account (account_id, kind, display_name, opened_at) "
            "VALUES ('x', 'CUSTODIAN', 'Not a kind', ?)",
            (SEED_NOW,),
        )


def test_connect_refuses_to_conjure_an_empty_database(tmp_path):
    """An empty ledger reads as a healthy system holding nothing, so a typo must raise."""
    with pytest.raises(FileNotFoundError, match="no authority database"):
        connect(tmp_path / "typo.db")
