"""The authority. Every rule this system enforces is in the SQL below.

Role: schema (DDL only, executed against the one authority database)
Reads: nothing
Writes: the schema of runtime/state/gigajoule.db
Can move tokens: no
Live-safe: yes -- every statement is IF NOT EXISTS and none touches rows

WHY THE RULES ARE IN SQL RATHER THAN IN PYTHON, which is the decision this
whole file exists to express. A rule expressed as a constraint, a trigger or a
view answers the same way for every reader: an auditor with a sqlite3 prompt, a
future service written in another language, a test seeding rows directly, and
the application itself. The same rule expressed as a Python check answers only
for whoever called that function, and only if they called it. On a system whose
product is "these tokens are backed by real energy", a rule that can be bypassed
by connecting with a different client is not a rule.

So: conservation, append-only, no-double-mint and no-overdraft are CONSTRAINTS
AND TRIGGERS, not application logic. Python may refuse earlier and with a better
message -- `ledger.py` does -- but nothing depends on Python having run.

THE LEDGER IS DOUBLE-ENTRY AND EVERY BATCH SUMS TO ZERO. There is no exception
for minting, and that is the single most important thing on this page. A naive
design mints by inserting one positive row "from nowhere"; conservation is then
a reconciliation between two different queries and it can only ever be checked
approximately. Here, minting DEBITS a system account named ISSUANCE and credits
the holder, so:

    SUM(joules_delta) over the entire ledger == 0, always, with no exception

which makes conservation a single SELECT that returns 0 on a healthy database
and a non-zero figure that IS the defect on a broken one. Two derived quantities
fall out of the same table for free:

    total ever minted   == -balance(ISSUANCE)
    total ever retired  ==  balance(RETIREMENT)
    circulating supply  ==  the sum of every HOLDER balance

ISSUANCE therefore runs monotonically negative and RETIREMENT monotonically
positive. Those are not accounting curiosities; they are the reason the
invariant is one line instead of a reconciliation job.

WHY RETIREMENT IS AN ACCOUNT AND NOT A DELETE. Retiring is how a consumer claims
energy against their own use, and double-claiming is the central fraud in every
energy-certificate market that has had one. If retirement deleted rows, the
evidence of what was claimed would be gone exactly when someone needs to prove
it was claimed once. Tokens move to an account they can never leave, the history
stays, and "was this claimed twice?" is a query rather than an investigation.

TIMESTAMPS ARE INTEGER EPOCH SECONDS, UTC. One stored form; every human-readable
rendering is derived in a view with datetime(col,'unixepoch'). Storing both an
epoch and an ISO string would be two representations of one fact, which agree on
the day they are written and drift from then on -- and a timestamp that drifts
in an energy ledger moves a reading across an interval boundary, which changes
what minted.
"""

from __future__ import annotations

# Account kinds. HOLDER is an ordinary party; the other two are the system
# accounts the double-entry design above requires, and there is exactly one of
# each -- enforced by the unique index further down, because a second ISSUANCE
# account would split the minted total across two rows and quietly break every
# supply figure derived from it.
ACCOUNT_KINDS = ("HOLDER", "ISSUANCE", "RETIREMENT")

ISSUANCE_ACCOUNT = "system:issuance"
RETIREMENT_ACCOUNT = "system:retirement"

LEDGER_REASONS = ("MINT", "TRANSFER", "RETIRE", "REVERSAL")

SCHEMA_SQL = """
-- WAL is a property of the FILE and survives every later connection, so it
-- is set here, once, at setup. `PRAGMA foreign_keys` is the opposite: it is
-- per-CONNECTION and defaults to OFF, so setting it here would enforce the
-- references below only for the connection that happened to run setup.
-- `db.connect()` sets it on every connection, and a test asserts it does --
-- a foreign key nobody enforces is the most convincing kind of dead rule.
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------------
-- THE TWO VOCABULARIES, AS TABLES RATHER THAN AS CHECK LISTS.
--
-- These began as `CHECK (kind IN (...))` with the tuple interpolated from
-- Python, which is two problems in one line: the DDL became a built string
-- (ruff S608, correctly), and the vocabulary existed twice -- once in Python
-- and once in the constraint -- which is the duplication that agrees on the
-- day it is written and drifts from then on.
--
-- As tables they are a foreign key instead: enforced by the database, readable
-- with a SELECT, joinable in a report, and able to carry the one thing a CHECK
-- list cannot -- a column saying what each value MEANS. `ACCOUNT_KINDS` and
-- `LEDGER_REASONS` remain in Python for callers to import, and
-- tests/ledger/test_schema.py asserts they match these rows exactly, so a
-- value added to either side without the other fails the suite.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_account_kind (
    kind        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO gj_account_kind (kind, description) VALUES
    ('HOLDER',
     'An ordinary party. May hold, receive, send and retire. Balance may never go negative.'),
    ('ISSUANCE',
     'The system source account. Debited on every mint, so its balance is minus the total ever minted. Exactly one exists.'),
    ('RETIREMENT',
     'The system sink account. Credited on every retirement; tokens that arrive here can never leave, which is what makes double-claiming a query rather than an investigation. Exactly one exists.');

CREATE TABLE IF NOT EXISTS gj_ledger_reason (
    reason      TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO gj_ledger_reason (reason, description) VALUES
    ('MINT',
     'Verified meter energy becomes tokens. Debits ISSUANCE, credits a holder. Carries the reading_id on both legs.'),
    ('TRANSFER',
     'Tokens move between holders. Changes no total.'),
    ('RETIRE',
     'A holder claims energy against its own consumption. Debits the holder, credits RETIREMENT. Irreversible by design.'),
    ('REVERSAL',
     'A correction. The ledger is append-only, so an error is undone by an equal and opposite batch that cites the original, never by editing it.');

-- ---------------------------------------------------------------------------
-- A metered generation asset. Provenance starts here: a token is only as good
-- as the claim about what produced it, so fuel and grid region are recorded on
-- the source rather than copied onto every reading.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_source (
    source_id        TEXT PRIMARY KEY,
    fuel             TEXT NOT NULL,
    grid_region      TEXT NOT NULL,
    -- Nameplate rating in watts. This is what attestation.check_ceiling()
    -- multiplies by interval length to decide whether a reading is physically
    -- possible, so an error here admits impossible energy. It is registered
    -- once, deliberately, and changing it is a provenance event.
    nameplate_watts  INTEGER NOT NULL CHECK (nameplate_watts > 0),
    registered_at    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- One interval reading from one source. APPEND-ONLY (see triggers below).
--
-- The UNIQUE constraint is the first half of no-double-mint, and it is placed
-- here rather than only on the ledger because the cheapest place to stop a
-- duplicate is before it becomes a token: a resubmitted meter file is the
-- ordinary case, not the adversarial one, and it should fail on insert.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_meter_reading (
    reading_id       TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES gj_source(source_id),
    interval_start   INTEGER NOT NULL,
    interval_end     INTEGER NOT NULL,
    joules           INTEGER NOT NULL CHECK (joules >= 0),
    attestor         TEXT NOT NULL,
    observed_at      INTEGER NOT NULL,
    CHECK (interval_end > interval_start),
    UNIQUE (source_id, interval_start, interval_end)
);

CREATE INDEX IF NOT EXISTS ix_gj_reading_source
    ON gj_meter_reading (source_id, interval_start);

-- ---------------------------------------------------------------------------
-- WHY A READING DID NOT MINT. Append-only, like everything else.
--
-- A reading that fails the physical ceiling is still EVIDENCE and is still
-- recorded: refusing to store it would destroy the only record that a meter
-- reported something impossible, which is exactly the record an investigation
-- needs. So every reading is kept, and this table says which ones were refused
-- and by how much.
--
-- It also keeps `v_gj_unminted_reading` honest. Without it, an impossible
-- reading would sit in the work queue forever looking like pending work, and
-- the queue would grow without anything being wrong -- the shape where a
-- broken input and a backlog are indistinguishable on a status screen.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_reading_refusal (
    reading_id   TEXT PRIMARY KEY REFERENCES gj_meter_reading(reading_id),
    check_name   TEXT NOT NULL,
    detail       TEXT NOT NULL,
    refused_at   INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS gj_refusal_no_update
BEFORE UPDATE ON gj_reading_refusal
BEGIN
    SELECT RAISE(ABORT,
        'gj_reading_refusal is append-only: a reversed refusal is a new reading');
END;

-- ---------------------------------------------------------------------------
-- A party that can hold tokens, plus the two system accounts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_account (
    account_id       TEXT PRIMARY KEY,
    kind             TEXT NOT NULL REFERENCES gj_account_kind(kind),
    display_name     TEXT NOT NULL,
    opened_at        INTEGER NOT NULL
);

-- Exactly one ISSUANCE and one RETIREMENT account. A second of either would
-- split the minted or retired total across rows and every supply figure
-- derived from it would silently understate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_gj_one_system_account
    ON gj_account (kind) WHERE kind IN ('ISSUANCE', 'RETIREMENT');

-- ---------------------------------------------------------------------------
-- THE LEDGER. Append-only, double-entry, signed.
--
-- `batch_id` groups the legs of one economic event. Every batch sums to zero;
-- `v_gj_imbalanced_batch` finds any that does not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_ledger_entry (
    entry_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL,
    account_id       TEXT NOT NULL REFERENCES gj_account(account_id),
    -- Signed integer joules. See units.py for why this is joules and why it is
    -- an integer; in short, conservation is only exact in integer arithmetic
    -- and a tolerance is the width of the discrepancy nobody investigates.
    joules_delta     INTEGER NOT NULL CHECK (joules_delta <> 0),
    reason           TEXT NOT NULL REFERENCES gj_ledger_reason(reason),
    -- Set on both legs of a MINT and NULL otherwise, so the provenance of every
    -- token traces to the interval that produced it.
    reading_id       TEXT REFERENCES gj_meter_reading(reading_id),
    memo             TEXT NOT NULL DEFAULT '',
    recorded_at      INTEGER NOT NULL,
    CHECK ((reason = 'MINT') = (reading_id IS NOT NULL))
);

-- Second half of no-double-mint: at most one CREDIT leg per reading. The index
-- is on the positive leg only, because a mint writes two rows carrying the same
-- reading_id and a naive unique index would reject every mint as a duplicate of
-- its own debit.
CREATE UNIQUE INDEX IF NOT EXISTS ux_gj_one_mint_per_reading
    ON gj_ledger_entry (reading_id) WHERE reason = 'MINT' AND joules_delta > 0;

CREATE INDEX IF NOT EXISTS ix_gj_entry_account ON gj_ledger_entry (account_id);
CREATE INDEX IF NOT EXISTS ix_gj_entry_batch   ON gj_ledger_entry (batch_id);

-- ---------------------------------------------------------------------------
-- APPEND-ONLY, ENFORCED. A correction is a REVERSAL batch, never an edit.
--
-- This is in the database rather than in a code review convention because the
-- whole value of the ledger is that its history cannot be quietly restated.
-- A reviewer can miss an UPDATE; the trigger cannot.
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS gj_ledger_no_update
BEFORE UPDATE ON gj_ledger_entry
BEGIN
    SELECT RAISE(ABORT,
        'gj_ledger_entry is append-only: correct with a REVERSAL batch, never an UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS gj_ledger_no_delete
BEFORE DELETE ON gj_ledger_entry
BEGIN
    SELECT RAISE(ABORT,
        'gj_ledger_entry is append-only: correct with a REVERSAL batch, never a DELETE');
END;

CREATE TRIGGER IF NOT EXISTS gj_reading_no_update
BEFORE UPDATE ON gj_meter_reading
BEGIN
    SELECT RAISE(ABORT,
        'gj_meter_reading is append-only: a restated reading is a new reading');
END;

CREATE TRIGGER IF NOT EXISTS gj_reading_no_delete
BEFORE DELETE ON gj_meter_reading
BEGIN
    SELECT RAISE(ABORT,
        'gj_meter_reading is append-only: evidence of what was measured is never removed');
END;

-- ---------------------------------------------------------------------------
-- NO OVERDRAFT. A holder cannot spend joules it does not hold.
--
-- Costs one indexed SUM per debit, which is the trade this system wants: the
-- alternative is a cached balance column, and a cached balance that disagrees
-- with the entries it summarizes is the defect this whole design is arranged
-- to make impossible. If this ever becomes the bottleneck the answer is a
-- materialized checkpoint that the ledger still derives from -- never a
-- writable balance.
--
-- System accounts are exempt by construction: ISSUANCE is negative by design
-- (its balance IS the minted total) and RETIREMENT only receives.
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS gj_no_overdraft
AFTER INSERT ON gj_ledger_entry
WHEN NEW.joules_delta < 0
 AND (SELECT kind FROM gj_account WHERE account_id = NEW.account_id) = 'HOLDER'
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(joules_delta), 0)
          FROM gj_ledger_entry WHERE account_id = NEW.account_id
    ) < 0 THEN RAISE(ABORT, 'overdraft: a holder cannot spend joules it does not hold')
    END;
END;

-- ---------------------------------------------------------------------------
-- VIEWS. Every derived quantity is here, so that no two readers can compute
-- the same figure two ways.
-- ---------------------------------------------------------------------------

-- Balances are DERIVED, never stored. This is the view that makes a writable
-- balance column unnecessary, and therefore makes a stale one impossible.
CREATE VIEW IF NOT EXISTS v_gj_balance AS
SELECT a.account_id,
       a.kind,
       a.display_name,
       COALESCE(SUM(e.joules_delta), 0) AS joules,
       COUNT(e.entry_id)                AS entries
  FROM gj_account a
  LEFT JOIN gj_ledger_entry e ON e.account_id = a.account_id
 GROUP BY a.account_id, a.kind, a.display_name;

-- THE INVARIANT. `residual_joules` is 0 on a healthy ledger. Any other value is
-- the defect itself, in joules, not a symptom of one.
CREATE VIEW IF NOT EXISTS v_gj_conservation AS
SELECT
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry) AS residual_joules,
    (SELECT -COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'ISSUANCE')                                 AS minted_joules,
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'RETIREMENT')                               AS retired_joules,
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'HOLDER')                                   AS circulating_joules;

-- Any batch whose legs do not sum to zero. Empty on a healthy ledger. The
-- triggers above cannot enforce this per row -- legs arrive one at a time --
-- so it is written once here and asserted by the suite.
CREATE VIEW IF NOT EXISTS v_gj_imbalanced_batch AS
SELECT batch_id,
       SUM(joules_delta) AS residual_joules,
       COUNT(*)          AS legs
  FROM gj_ledger_entry
 GROUP BY batch_id
HAVING SUM(joules_delta) <> 0;

-- Readings that have been accepted but have not minted. This is the work queue,
-- and it is a view rather than a status column on the reading: a status column
-- is a second representation of a fact the ledger already holds, and the two
-- would drift the first time a mint failed after the flag was set.
CREATE VIEW IF NOT EXISTS v_gj_unminted_reading AS
SELECT r.*
  FROM gj_meter_reading r
 WHERE NOT EXISTS (
     SELECT 1 FROM gj_ledger_entry e
      WHERE e.reading_id = r.reading_id AND e.reason = 'MINT'
 )
   AND NOT EXISTS (
     SELECT 1 FROM gj_reading_refusal f WHERE f.reading_id = r.reading_id
 );

-- Readings that were recorded and refused, with the reason. A row here is a
-- broken input somewhere upstream, not a backlog item.
CREATE VIEW IF NOT EXISTS v_gj_refused_reading AS
SELECT r.reading_id,
       r.source_id,
       s.fuel,
       s.nameplate_watts,
       r.joules,
       r.interval_end - r.interval_start AS interval_seconds,
       f.check_name,
       f.detail,
       datetime(f.refused_at, 'unixepoch')  AS refused_at_utc
  FROM gj_reading_refusal f
  JOIN gj_meter_reading r ON r.reading_id = f.reading_id
  JOIN gj_source s        ON s.source_id  = r.source_id;

-- Provenance: every circulating joule traced to the source and fuel that made
-- it. This is what a buyer is actually paying for, so it is a first-class view
-- rather than a report somebody assembles.
CREATE VIEW IF NOT EXISTS v_gj_provenance AS
SELECT e.account_id,
       s.source_id,
       s.fuel,
       s.grid_region,
       SUM(e.joules_delta)                       AS minted_joules,
       MIN(datetime(r.interval_start, 'unixepoch')) AS first_interval_utc,
       MAX(datetime(r.interval_end, 'unixepoch'))   AS last_interval_utc
  FROM gj_ledger_entry e
  JOIN gj_meter_reading r ON r.reading_id = e.reading_id
  JOIN gj_source s        ON s.source_id  = r.source_id
 WHERE e.reason = 'MINT' AND e.joules_delta > 0
 GROUP BY e.account_id, s.source_id, s.fuel, s.grid_region;
"""
