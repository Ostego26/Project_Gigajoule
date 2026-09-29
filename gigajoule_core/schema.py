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

# Account kinds. HOLDER is an ordinary party, STORAGE is a battery or other
# asset that takes title to energy it holds, and the rest are the system accounts
# the double-entry design above requires. There is exactly one of each system
# account -- enforced by the unique index further down, because a second ISSUANCE
# would split the minted total across two rows and quietly break every supply
# figure derived from it.
ACCOUNT_KINDS = ("HOLDER", "STORAGE", "ISSUANCE", "RETIREMENT", "LOSS", "CONVERSION")

ISSUANCE_ACCOUNT = "system:issuance"
RETIREMENT_ACCOUNT = "system:retirement"
LOSS_ACCOUNT = "system:loss"
CONVERSION_ACCOUNT = "system:conversion"

# The system accounts, in the order bootstrap creates them. One tuple rather
# than four literals, so a fifth cannot be added by being typed into a second
# place (which is how a vocabulary starts to disagree with itself).
SYSTEM_ACCOUNTS = (
    (ISSUANCE_ACCOUNT, "ISSUANCE", "System issuance"),
    (RETIREMENT_ACCOUNT, "RETIREMENT", "System retirement"),
    (LOSS_ACCOUNT, "LOSS", "System loss"),
    (CONVERSION_ACCOUNT, "CONVERSION", "System lot conversion"),
)

LEDGER_REASONS = ("MINT", "TRANSFER", "RETIRE", "REVERSAL", "CHARGE", "DISCHARGE")

# How a lot came to exist. GENERATION is a meter reading; STORAGE_RELEASE is
# energy that was already minted and has been moved in TIME by a battery.
LOT_ORIGINS = ("GENERATION", "STORAGE_RELEASE")

# Bumped whenever the schema changes shape. `db.connect()` refuses a database
# whose PRAGMA user_version does not match, because the alternative -- opening it
# anyway -- means a query silently reads a column that is not there, or worse,
# one that means something different than it used to.
SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA user_version = 2;  -- must equal SCHEMA_VERSION; test_schema.py reads it back
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
     'The system sink account. Credited on every retirement; tokens that arrive here can never leave, which is what makes double-claiming a query rather than an investigation. Exactly one exists.'),
    ('STORAGE',
     'A battery or other asset that takes title to the energy it holds. Bounded by a capacity; charging past it is refused. Balance may never go negative, same as a holder.'),
    ('LOSS',
     'The system sink for energy destroyed by physics rather than consumed by anyone -- round-trip storage loss today, transmission loss later. Credited on discharge so that conservation stays exact instead of being written with a tolerance.'),
    ('CONVERSION',
     'The system bridge between lots. Energy moved from one lot to another passes through here, positive in the old lot and negative in the new, so that every lot sums to zero across accounts. Its TOTAL balance is always zero; a non-zero total is a defect.');

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
     'A correction. The ledger is append-only, so an error is undone by an equal and opposite batch that cites the original, never by editing it.'),
    ('CHARGE',
     'Energy sold into a storage asset. An ordinary transfer of title; the lot is unchanged, because nothing has happened to the energy yet.'),
    ('DISCHARGE',
     'Energy released from storage. Burns the stored lot, creates a new lot with the RELEASE time as its vintage, and sends the round-trip loss to LOSS. The new lot is the point: energy released at 8pm is not energy generated at noon.');

CREATE TABLE IF NOT EXISTS gj_lot_origin (
    origin      TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO gj_lot_origin (origin, description) VALUES
    ('GENERATION',
     'Minted from a verified meter reading. Its vintage is the interval the energy was actually generated in.'),
    ('STORAGE_RELEASE',
     'Created by a battery discharge from a parent lot. Its vintage is the RELEASE window, not the parent generation window, because that is when the energy was delivered to the grid.');

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
-- A LOT: a parcel of energy with one provenance and one vintage. The unit that
-- makes this a registry rather than a balance sheet.
--
-- WHY HOLDINGS ARE PER-LOT AND NOT A SINGLE NUMBER. A solar joule and a coal
-- joule are both a joule, and they are not the same good. Neither is a joule
-- generated at noon and one generated at 3am. Every use this system is being
-- built for turns on that distinction: a buyer paying a premium for renewable
-- energy is paying for the attribute, not the quantity, and 24/7 carbon-free
-- accounting is entirely a claim about WHEN. Collapse holdings to one number and
-- the attribute is gone the first time anyone transfers anything.
--
-- The cost is that payment stops being "subtract N". It becomes "select lots
-- totalling N", which is what `lots.select_lots()` does and why that decision
-- lives in its own module with its own tests. Real commodity and securities
-- settlement works exactly this way, for exactly this reason.
--
-- VINTAGE IS WHEN THE ENERGY WAS DELIVERABLE, not when the row was written. For
-- a generation lot that is the meter interval. For a storage release it is the
-- RELEASE window -- see `gj_lot_origin` and the DISCHARGE reason for why that
-- distinction is the whole point of letting a battery into this ledger.
--
-- EVERY LOT SUMS TO ZERO ACROSS ACCOUNTS, exactly as the whole ledger does. A
-- mint debits ISSUANCE and credits the holder IN THE SAME LOT; a discharge moves
-- joules between two lots through the CONVERSION account so that both still
-- balance. `v_gj_imbalanced_lot` is empty on a healthy ledger, and it catches a
-- class of bug that global conservation cannot see: crediting a new lot without
-- debiting the old one sums to zero globally and is still energy appearing from
-- nowhere with a different provenance attached.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_lot (
    lot_id         TEXT PRIMARY KEY,
    origin         TEXT NOT NULL REFERENCES gj_lot_origin(origin),
    -- Exactly one of these is set, per the CHECK below: a generation lot cites
    -- the reading that produced it, a storage release cites the lot it came from.
    reading_id     TEXT REFERENCES gj_meter_reading(reading_id),
    parent_lot_id  TEXT REFERENCES gj_lot(lot_id),
    -- Provenance carried on the lot rather than looked up through the reading,
    -- because a storage-release lot has no reading and still has a fuel: energy
    -- that went into a battery as solar comes out as solar, moved in time.
    fuel           TEXT NOT NULL,
    grid_region    TEXT NOT NULL,
    vintage_start  INTEGER NOT NULL,
    vintage_end    INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    CHECK (vintage_end > vintage_start),
    CHECK ((origin = 'GENERATION')      = (reading_id    IS NOT NULL)),
    CHECK ((origin = 'STORAGE_RELEASE') = (parent_lot_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_gj_lot_vintage ON gj_lot (vintage_start, lot_id);

-- One generation lot per reading. The reading table already refuses a duplicate
-- interval and the ledger already refuses a second mint; this is the same rule
-- at the third place it can be broken, which is the place a future importer that
-- builds lots directly would break it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_gj_one_lot_per_reading
    ON gj_lot (reading_id) WHERE reading_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- A STORAGE ASSET: a battery. Holds energy it has taken title to, and gives
-- back less than it took.
--
-- ROUND-TRIP EFFICIENCY IS INTEGER BASIS POINTS, not a float. 8800 is 88.00%.
-- The same argument as units.py: this number multiplies every joule that passes
-- through the asset, and a float here would put a rounding residue into the one
-- calculation whose output is compared against an exact conservation check.
--
-- IT IS ONE NUMBER, APPLIED ON DISCHARGE, and that is a deliberate simplification
-- with a reason. Physically the loss splits between charging and discharging, but
-- round-trip efficiency is what a manufacturer publishes and what a test measures;
-- splitting it into two factors would mean inventing two numbers nobody has
-- measured in order to look more precise. Applied on discharge because that is
-- when the loss is realized and when there is a delivered quantity to compare it
-- against.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gj_storage_asset (
    asset_id            TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL UNIQUE REFERENCES gj_account(account_id),
    grid_region         TEXT NOT NULL,
    capacity_joules     INTEGER NOT NULL CHECK (capacity_joules > 0),
    round_trip_bp       INTEGER NOT NULL CHECK (round_trip_bp > 0 AND round_trip_bp <= 10000),
    registered_at       INTEGER NOT NULL
);

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
    -- EVERY entry names a lot. Not nullable, and that is the point: an entry
    -- without a lot would be joules with no provenance, which is the thing this
    -- system exists not to have. A transfer moves within a lot; a discharge is
    -- two lots bridged through CONVERSION.
    lot_id           TEXT NOT NULL REFERENCES gj_lot(lot_id),
    -- Set on both legs of a MINT and NULL otherwise, so the provenance of every
    -- token traces to the interval that produced it. Redundant with the lot's own
    -- reading_id by construction, and kept because the unique index below is what
    -- makes a second mint impossible and it has to key on something in this table.
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

CREATE INDEX IF NOT EXISTS ix_gj_entry_account ON gj_ledger_entry (account_id, lot_id);
CREATE INDEX IF NOT EXISTS ix_gj_entry_lot     ON gj_ledger_entry (lot_id);
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
-- IT IS PER (ACCOUNT, LOT), NOT PER ACCOUNT, and the difference is a real rule
-- rather than a refinement. Checked per account, a holder sitting on 1GJ of coal
-- could spend 1GJ of solar it does not have and the account total would stay
-- non-negative the whole way -- the ledger would report a sound balance while
-- having delivered an attribute that was never generated. Provenance is the
-- product here, so the constraint has to bind at the level provenance lives at.
--
-- System accounts are exempt by construction: ISSUANCE is negative by design
-- (its balance IS the minted total), RETIREMENT and LOSS only receive, and
-- CONVERSION is a bridge that is negative in the new lot and positive in the old
-- within a single batch.
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS gj_no_overdraft
AFTER INSERT ON gj_ledger_entry
WHEN NEW.joules_delta < 0
 AND (SELECT kind FROM gj_account WHERE account_id = NEW.account_id) IN ('HOLDER', 'STORAGE')
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(joules_delta), 0)
          FROM gj_ledger_entry
         WHERE account_id = NEW.account_id AND lot_id = NEW.lot_id
    ) < 0 THEN RAISE(ABORT,
        'overdraft: an account cannot spend joules it does not hold IN THAT LOT')
    END;
END;

-- ---------------------------------------------------------------------------
-- A BATTERY CANNOT HOLD MORE THAN IT CAN HOLD.
--
-- Checked on the account TOTAL rather than per lot, because capacity is physical
-- -- a cell does not care which lot the electrons came from. That makes it the
-- one rule in this schema deliberately coarser than per-lot, and it is coarser
-- because the thing it models is.
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS gj_storage_capacity
AFTER INSERT ON gj_ledger_entry
WHEN NEW.joules_delta > 0
 AND (SELECT kind FROM gj_account WHERE account_id = NEW.account_id) = 'STORAGE'
BEGIN
    SELECT CASE WHEN (
        SELECT COALESCE(SUM(joules_delta), 0)
          FROM gj_ledger_entry WHERE account_id = NEW.account_id
    ) > (SELECT capacity_joules FROM gj_storage_asset WHERE account_id = NEW.account_id)
    THEN RAISE(ABORT, 'storage capacity exceeded: the asset cannot hold this much energy')
    END;
END;

CREATE TRIGGER IF NOT EXISTS gj_lot_no_update
BEFORE UPDATE ON gj_lot
BEGIN
    SELECT RAISE(ABORT,
        'gj_lot is append-only: a lot''s provenance and vintage are what it IS');
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

-- THE INVARIANT. Both residual columns are 0 on a healthy ledger, and any other
-- value is the defect itself, in joules, rather than a symptom of one.
--
-- `residual_joules` is the whole ledger summed: every batch is double-entry, so
-- the total must be zero.
--
-- `supply_residual_joules` is the SUPPLY IDENTITY -- minted equals circulating
-- plus stored plus retired plus lost -- and it is computed HERE rather than by
-- each caller, which is why the view is wrapped in an outer SELECT. That is not
-- style. The identity was originally written out at each reader, and when
-- storage added two destinations the version in `./gigajoule verify` was not
-- updated: the CLI reported FAIL on a demonstrably healthy ledger while the test
-- suite reported PASS, because each held its own copy of the same rule and one
-- of them was a version behind. Found by running the command, not by a test --
-- the tests had the correct copy.
CREATE VIEW IF NOT EXISTS v_gj_conservation AS
SELECT *,
       minted_joules
         - (circulating_joules + stored_joules + retired_joules + lost_joules)
         AS supply_residual_joules
  FROM (
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
      WHERE a.kind = 'HOLDER')                                   AS circulating_joules,
    -- Energy sitting in batteries. Still in existence, still backed, not yet
    -- delivered to anyone -- so it is neither circulating nor retired and needs
    -- its own column or the identity below does not close.
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'STORAGE')                                  AS stored_joules,
    -- Energy destroyed by physics rather than consumed by anyone. Round-trip
    -- storage loss today. This column is why the conservation check can stay
    -- exact once batteries exist instead of acquiring a tolerance.
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'LOSS')                                     AS lost_joules,
    -- Always zero. CONVERSION bridges lots and nets out within each batch, so a
    -- non-zero total here means a discharge wrote one side of the bridge.
    (SELECT COALESCE(SUM(joules_delta), 0) FROM gj_ledger_entry e
       JOIN gj_account a ON a.account_id = e.account_id
      WHERE a.kind = 'CONVERSION')                               AS conversion_joules
);

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

-- Position per (account, lot). The real balance in this system; `v_gj_balance`
-- above is its sum and is what a payment screen shows.
CREATE VIEW IF NOT EXISTS v_gj_lot_balance AS
SELECT account_id,
       lot_id,
       SUM(joules_delta) AS joules
  FROM gj_ledger_entry
 GROUP BY account_id, lot_id;

-- Any lot whose entries do not sum to zero across all accounts. Empty on a
-- healthy ledger, and it catches what global conservation cannot: crediting a
-- new lot without debiting the old one nets to zero overall and is still energy
-- appearing from nowhere wearing a different provenance.
CREATE VIEW IF NOT EXISTS v_gj_imbalanced_lot AS
SELECT lot_id,
       SUM(joules_delta) AS residual_joules,
       COUNT(*)          AS entries
  FROM gj_ledger_entry
 GROUP BY lot_id
HAVING SUM(joules_delta) <> 0;

-- What an account actually holds, enriched with everything a buyer or a lot
-- selector needs. Positive positions only: a lot spent down to zero is history,
-- not a holding, and leaving it in makes every selection query filter it out.
CREATE VIEW IF NOT EXISTS v_gj_holding AS
SELECT b.account_id,
       b.lot_id,
       b.joules,
       l.origin,
       l.fuel,
       l.grid_region,
       l.vintage_start,
       l.vintage_end,
       datetime(l.vintage_start, 'unixepoch') AS vintage_start_utc,
       datetime(l.vintage_end, 'unixepoch')   AS vintage_end_utc
  FROM v_gj_lot_balance b
  JOIN gj_lot l ON l.lot_id = b.lot_id
 WHERE b.joules > 0;

-- Every battery, its charge level and its headroom. `capacity_joules` is the
-- physical bound the gj_storage_capacity trigger enforces; `headroom_joules` is
-- what an operator needs before scheduling a charge.
CREATE VIEW IF NOT EXISTS v_gj_storage_state AS
SELECT s.asset_id,
       s.account_id,
       s.grid_region,
       s.capacity_joules,
       s.round_trip_bp,
       COALESCE(b.joules, 0)                        AS charged_joules,
       s.capacity_joules - COALESCE(b.joules, 0)    AS headroom_joules,
       COUNT(h.lot_id)                              AS lots_held
  FROM gj_storage_asset s
  LEFT JOIN v_gj_balance b ON b.account_id = s.account_id
  LEFT JOIN v_gj_holding h ON h.account_id = s.account_id
 GROUP BY s.asset_id, s.account_id, s.grid_region, s.capacity_joules,
          s.round_trip_bp, b.joules;

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
-- Provenance of what is held RIGHT NOW, traced through lots rather than through
-- mint entries. Reading it off the mints was correct while every lot came from a
-- meter and nothing ever moved; it reports the wrong thing the moment a battery
-- exists, because a storage release has no reading and would simply vanish from
-- the answer. Held positions are also the question a buyer actually asks --
-- "what am I holding and where did it come from" -- rather than "what was once
-- minted to me".
CREATE VIEW IF NOT EXISTS v_gj_provenance AS
SELECT h.account_id,
       h.fuel,
       h.grid_region,
       h.origin,
       SUM(h.joules)              AS joules,
       COUNT(*)                   AS lots,
       MIN(h.vintage_start_utc)   AS earliest_vintage_utc,
       MAX(h.vintage_end_utc)     AS latest_vintage_utc
  FROM v_gj_holding h
 GROUP BY h.account_id, h.fuel, h.grid_region, h.origin;

-- Where a storage-release lot's energy originally came from. One hop today,
-- because a battery cannot yet charge from another battery's output; when it
-- can, this becomes the recursive CTE that walks the chain.
CREATE VIEW IF NOT EXISTS v_gj_lot_lineage AS
SELECT child.lot_id,
       child.origin,
       child.vintage_start        AS released_at,
       parent.lot_id              AS parent_lot_id,
       parent.vintage_start       AS generated_at,
       parent.reading_id          AS parent_reading_id,
       child.fuel,
       child.grid_region
  FROM gj_lot child
  JOIN gj_lot parent ON parent.lot_id = child.parent_lot_id;
"""
