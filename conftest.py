"""Test fixtures, and the one rule the whole suite is arranged around.

Role: test support
Reads: nothing
Writes: a temporary database per test
Can move tokens: no
Live-safe: yes -- every fixture here builds a throwaway database

VERIFY BY BEHAVIOR, NEVER BY SQL TEXT. Every test in this suite seeds real rows
into the real tables, runs the real function, and asserts on the rows the real
views produce. None of them asserts that a string appears in a schema, and none
reimplements a rule in order to check it.

The reason is specific and it is not fussiness. A rule expressed as a trigger, a
constraint or a view is only enforced if the database actually enforces it, and
the ways that silently fails -- a pragma left off, an index whose partial WHERE
does not match what was intended, a trigger that fires on the wrong event -- all
look completely correct when you read the DDL. "The schema contains the word
TRIGGER" proves nothing. "Seeding an overdraft raised, and the balance view
still reads what it read before" proves the thing that matters.

EVERY TEST GETS ITS OWN DATABASE, IN A TMPDIR, AND NEVER TOUCHES
`runtime/state/gigajoule.db`. `ledger()` takes an explicit path and there is no
module-global default to fall back to, so a test cannot reach the real ledger by
forgetting an argument -- which is the failure where a test's first statement,
a DELETE or a seed, lands on live state.

ONE DATABASE PER TEST, NOT ONE PER CASE. Building a SQLite file is cheap but it
is not free, and a suite that builds three hundred of them to assert three
hundred things is the same coverage with a tax on it. Where a test needs many
scenarios, seed them into one database and assert across it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from gigajoule_core import ledger
from gigajoule_core.db import open_ledger

# A fixed instant, so that every seeded timestamp in the suite is deliberate.
# Deliberately NOT time.time(): a test that seeds "now" and asserts against a
# window measured off the wall clock passes every day until the day the window
# moves past it, and then fails for a reason that has nothing to do with the
# code it covers.
SEED_NOW = 1_800_000_000
HOUR = 3600


@pytest.fixture
def ledger_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh authority database with the schema applied and system accounts open."""
    conn = open_ledger(tmp_path / "gigajoule.db")
    ledger.bootstrap_system_accounts(conn, now=SEED_NOW)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def seeded(ledger_db: sqlite3.Connection) -> sqlite3.Connection:
    """A ledger with one 1kW solar source and one holder, ready to mint into.

    1,000 W is chosen so the arithmetic in every test that uses it is checkable
    by hand: over one hour the physical ceiling is exactly 3,600,000J, which is
    exactly 1kWh, which is exactly 0.0036GJ.
    """
    ledger.register_source(
        ledger_db,
        ledger.Source(
            source_id="solar-01", fuel="solar", grid_region="ERCOT", nameplate_watts=1_000
        ),
        now=SEED_NOW,
    )
    ledger.open_account(ledger_db, "acme", "Acme Power", now=SEED_NOW)
    return ledger_db
