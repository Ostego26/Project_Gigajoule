"""Minting, transfer and retirement. The only module that writes ledger rows.

Role: ledger operations
Reads: gj_source, gj_meter_reading, gj_account, gj_ledger_entry
Writes: gj_ledger_entry, gj_meter_reading, gj_reading_refusal, gj_account, gj_source
Can move tokens: YES
Live-safe: no -- every function here changes what exists

THIS IS THE ONE PLACE THAT WRITES TO THE LEDGER, and `_post_batch` is the one
function inside it that does. Every operation -- mint, transfer, retire, reverse
-- is expressed as a `Batch` of signed legs handed to that function, which checks
they sum to zero and writes them in a single transaction. Two implementations of
"write a balanced batch" would agree on the day they were written and drift from
then on, and the drift would be invisible: each would look correct in its own
function, and nothing would fail until a balance computed through one
contradicted a balance computed through the other.

WHY THE API IS "DESCRIBE THE THING, THEN POST IT". `Source`, `MeterReading`,
`Movement` and `Batch` are records rather than argument lists, which costs one
extra line at each call site and buys two things an auditable ledger wants. The
intent exists as a value BEFORE anything is written, so it can be logged,
validated or held for approval without a half-applied write; and the shape of
what was posted is one object a test can build directly rather than a signature
a test has to reproduce positionally.

THE DATABASE DOES NOT TRUST THIS MODULE, and that is deliberate rather than
redundant. Every invariant enforced here is ALSO a constraint or trigger in
`schema.py`: append-only, one mint per reading, no overdraft. The checks here
exist to fail EARLIER and with a message naming what the caller did wrong; the
ones in SQL exist because nothing may depend on this module having run. A future
service in another language, an operator at a sqlite3 prompt, and a test seeding
rows directly all bypass this file, and none of them may bypass the rules.

EVERY WRITE IS `BEGIN IMMEDIATE`. Half a mint is a credited token with no
matching debit -- exactly the state `v_gj_conservation` exists to prove
impossible -- so a batch is all-or-nothing. IMMEDIATE rather than DEFERRED
because it takes the write lock up front: a DEFERRED transaction that reads a
balance, then blocks on the write, can be reading a balance another writer has
already changed by the time it commits.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from gigajoule_core.attestation import DEFAULT_TOLERANCE, check_ceiling
from gigajoule_core.schema import ISSUANCE_ACCOUNT, RETIREMENT_ACCOUNT


class LedgerError(RuntimeError):
    """A refused ledger operation.

    Raised rather than returning a falsy value because every caller is about to
    report what happened, and a bare None would leave "nothing to do" and "this
    was refused" indistinguishable in the output -- the failure shape where a
    skipped operation and a completed one read the same on a status screen.
    """


@dataclass(frozen=True)
class Leg:
    """One side of a balanced batch. Positive credits, negative debits."""

    account_id: str
    joules_delta: int


@dataclass(frozen=True)
class Batch:
    """A complete, balanced ledger event, before it is written.

    `residual()` is the check `_post_batch` runs, exposed so a caller can assert
    on a batch it has built without posting it.
    """

    legs: list[Leg]
    reason: str
    memo: str = ""
    reading_id: str | None = None

    def residual(self) -> int:
        """Joules unaccounted for. Zero on a well-formed batch."""
        return sum(leg.joules_delta for leg in self.legs)


@dataclass(frozen=True)
class Source:
    """A metered generation asset, as registered."""

    source_id: str
    fuel: str
    grid_region: str
    nameplate_watts: int


@dataclass(frozen=True)
class MeterReading:
    """One interval reading, as submitted by an attestor.

    `joules` is integer and that is not negotiable -- see `units.py` for why,
    and use `units.kwh_to_joules()` to get here from a meter's own figure rather
    than multiplying by 3.6e6 at the call site.
    """

    reading_id: str
    source_id: str
    interval_start: int
    interval_end: int
    joules: int
    attestor: str

    def interval_seconds(self) -> int:
        return self.interval_end - self.interval_start


@dataclass(frozen=True)
class Movement:
    """Tokens moving from one account to another."""

    from_account: str
    to_account: str
    joules: int
    memo: str = ""


@dataclass(frozen=True)
class MintOutcome:
    """What one pass of `mint_pending` did, so the caller can report it.

    Counts and reasons together: a pass that minted nothing because there was
    nothing to mint and a pass that minted nothing because everything was
    refused are different events, and a bare count cannot tell them apart.
    """

    minted: int = 0
    joules: int = 0
    skipped: list[str] = field(default_factory=list)


def _post_batch(conn: sqlite3.Connection, batch: Batch, *, now: int) -> str:
    """Write one balanced batch, atomically. The only writer of gj_ledger_entry.

    Refuses a batch whose legs do not sum to zero, before touching the database.
    `v_gj_imbalanced_batch` would find such a batch afterwards, but finding it
    afterwards means it is already in an append-only table that by design cannot
    be edited -- so the cheap check runs first and the view stays the proof
    rather than the detector.
    """
    if not batch.legs:
        raise LedgerError("refusing to post an empty batch")
    residual = batch.residual()
    if residual != 0:
        raise LedgerError(
            f"unbalanced batch: legs sum to {residual}J, not 0. Every batch is "
            "double-entry; energy is never created or destroyed by a ledger "
            "operation, only moved between accounts."
        )
    batch_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO gj_ledger_entry "
            "(batch_id, account_id, joules_delta, reason, reading_id, memo, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    leg.account_id,
                    leg.joules_delta,
                    batch.reason,
                    batch.reading_id,
                    batch.memo,
                    now,
                )
                for leg in batch.legs
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return batch_id


def open_account(
    conn: sqlite3.Connection,
    account_id: str,
    display_name: str,
    *,
    now: int,
    kind: str = "HOLDER",
) -> None:
    """Register a party that can hold tokens."""
    conn.execute(
        "INSERT INTO gj_account (account_id, kind, display_name, opened_at) VALUES (?, ?, ?, ?)",
        (account_id, kind, display_name, now),
    )


def bootstrap_system_accounts(conn: sqlite3.Connection, *, now: int) -> None:
    """Create the ISSUANCE and RETIREMENT accounts. Idempotent.

    Nothing can mint until these exist, because minting is a transfer FROM
    issuance rather than a creation from nothing -- which is what makes the
    conservation check a single SELECT.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO gj_account (account_id, kind, display_name, opened_at) "
        "VALUES (?, ?, ?, ?)",
        [
            (ISSUANCE_ACCOUNT, "ISSUANCE", "System issuance", now),
            (RETIREMENT_ACCOUNT, "RETIREMENT", "System retirement", now),
        ],
    )


def register_source(conn: sqlite3.Connection, source: Source, *, now: int) -> None:
    """Register a metered generation asset."""
    conn.execute(
        "INSERT INTO gj_source (source_id, fuel, grid_region, nameplate_watts, registered_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source.source_id, source.fuel, source.grid_region, source.nameplate_watts, now),
    )


def record_reading(
    conn: sqlite3.Connection,
    reading: MeterReading,
    *,
    now: int,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> bool:
    """Store one meter reading and decide whether it may mint.

    Returns True if the reading passed the physical ceiling and is now queued in
    `v_gj_unminted_reading`, False if it was recorded and refused.

    THE READING IS STORED EITHER WAY, and that is the point. A reading that
    reports more energy than its source can physically produce is evidence that
    something upstream is broken -- a nameplate typo, a Wh/kWh unit error, or an
    overstatement -- and discarding it would destroy the only record of the one
    thing an investigation needs to see. What the refusal does is keep it out of
    the mint queue, so a broken input never looks like a backlog.
    """
    source = conn.execute(
        "SELECT nameplate_watts FROM gj_source WHERE source_id = ?", (reading.source_id,)
    ).fetchone()
    if source is None:
        raise LedgerError(
            f"unknown source {reading.source_id!r}: register it before submitting "
            "readings, so that every token traces to a source whose nameplate and "
            "fuel are on record"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO gj_meter_reading "
            "(reading_id, source_id, interval_start, interval_end, joules, attestor, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                reading.reading_id,
                reading.source_id,
                reading.interval_start,
                reading.interval_end,
                reading.joules,
                reading.attestor,
                now,
            ),
        )
        verdict = check_ceiling(
            joules=reading.joules,
            nameplate_watts=source["nameplate_watts"],
            interval_seconds=reading.interval_seconds(),
            tolerance=tolerance,
        )
        if not verdict.allowed:
            conn.execute(
                "INSERT INTO gj_reading_refusal (reading_id, check_name, detail, refused_at) "
                "VALUES (?, ?, ?, ?)",
                (reading.reading_id, "physical_ceiling", verdict.reason(), now),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return verdict.allowed


def mint(conn: sqlite3.Connection, reading_id: str, *, to_account: str, now: int) -> str:
    """Turn one verified reading into tokens. Debits ISSUANCE, credits the holder.

    Refuses a reading that was refused at record time, and refuses one that has
    already minted. The second refusal is also a unique index in the schema --
    see the module docstring on why both exist.
    """
    row = conn.execute(
        "SELECT r.joules, "
        "       EXISTS(SELECT 1 FROM gj_reading_refusal f WHERE f.reading_id = r.reading_id) "
        "         AS refused, "
        "       EXISTS(SELECT 1 FROM gj_ledger_entry e "
        "               WHERE e.reading_id = r.reading_id AND e.reason = 'MINT') AS minted "
        "  FROM gj_meter_reading r WHERE r.reading_id = ?",
        (reading_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"no such reading: {reading_id!r}")
    if row["refused"]:
        raise LedgerError(
            f"reading {reading_id!r} was refused at record time and may not mint; "
            "see v_gj_refused_reading for which check refused it and by how much"
        )
    if row["minted"]:
        raise LedgerError(
            f"reading {reading_id!r} has already minted. One interval mints once, "
            "or the same energy is sold twice."
        )
    if row["joules"] == 0:
        raise LedgerError(
            f"reading {reading_id!r} measured 0J. A zero-energy interval is a valid "
            "measurement and is kept as evidence, but there is nothing to mint."
        )
    joules = int(row["joules"])
    return _post_batch(
        conn,
        Batch(
            legs=[Leg(ISSUANCE_ACCOUNT, -joules), Leg(to_account, joules)],
            reason="MINT",
            reading_id=reading_id,
            memo=f"mint from reading {reading_id}",
        ),
        now=now,
    )


def transfer(conn: sqlite3.Connection, movement: Movement, *, now: int) -> str:
    """Move tokens between holders. Changes no total."""
    if movement.joules <= 0:
        raise LedgerError(
            f"transfer amount must be positive, got {movement.joules}J. A negative "
            "transfer is a transfer in the other direction and must be written as "
            "one, so that the direction is legible in the ledger rather than in the sign."
        )
    if movement.from_account == movement.to_account:
        raise LedgerError(
            f"refusing a self-transfer on {movement.from_account!r}: it would write "
            "two legs that change no balance, and every later reader would have to "
            "work out that it meant nothing"
        )
    return _post_batch(
        conn,
        Batch(
            legs=[
                Leg(movement.from_account, -movement.joules),
                Leg(movement.to_account, movement.joules),
            ],
            reason="TRANSFER",
            memo=movement.memo,
        ),
        now=now,
    )


def retire(
    conn: sqlite3.Connection, *, account_id: str, joules: int, now: int, memo: str = ""
) -> str:
    """Claim energy against consumption. Irreversible by design.

    Tokens move to RETIREMENT, an account they can never leave. Retirement is
    not a delete: the history of what was claimed is the evidence that it was
    claimed once, and double-claiming is the fraud this market has to make
    checkable rather than merely forbidden.
    """
    if joules <= 0:
        raise LedgerError(f"retirement amount must be positive, got {joules}J")
    return _post_batch(
        conn,
        Batch(
            legs=[Leg(account_id, -joules), Leg(RETIREMENT_ACCOUNT, joules)],
            reason="RETIRE",
            memo=memo,
        ),
        now=now,
    )


def reverse(conn: sqlite3.Connection, batch_id: str, *, now: int, memo: str) -> str:
    """Undo a batch with an equal and opposite one. Never an edit.

    The ledger is append-only, so a correction ADDS history rather than
    restating it: both the error and its reversal stay visible, which is what
    lets an auditor see that a mistake was made and corrected rather than
    finding a ledger that has simply always been right.
    """
    legs = conn.execute(
        "SELECT account_id, joules_delta, reason FROM gj_ledger_entry WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    if not legs:
        raise LedgerError(f"no such batch: {batch_id!r}")
    if any(leg["reason"] == "RETIRE" for leg in legs):
        raise LedgerError(
            f"batch {batch_id!r} is a retirement and cannot be reversed. Retirement "
            "is the claim that energy was consumed; un-claiming it would return "
            "tokens to circulation against energy that is already spent."
        )
    return _post_batch(
        conn,
        Batch(
            legs=[Leg(leg["account_id"], -int(leg["joules_delta"])) for leg in legs],
            reason="REVERSAL",
            memo=f"reversal of {batch_id}: {memo}",
        ),
        now=now,
    )


def mint_pending(conn: sqlite3.Connection, *, to_account: str, now: int) -> MintOutcome:
    """Mint every queued reading, one batch each.

    ONE BATCH PER READING, DELIBERATELY, rather than one batch for the pass. A
    pass that is interrupted after twenty of fifty readings should leave twenty
    minted and thirty still queued -- which is what `v_gj_unminted_reading`
    reports on the next run with no reconciliation. Accumulating the pass and
    writing it at the end would turn every interruption into a total loss of the
    work done, and the queue would not even know it had happened.
    """
    outcome = MintOutcome()
    pending = conn.execute(
        "SELECT reading_id FROM v_gj_unminted_reading ORDER BY interval_start"
    ).fetchall()
    for row in pending:
        reading_id = row["reading_id"]
        try:
            mint(conn, reading_id, to_account=to_account, now=now)
        except LedgerError as exc:
            outcome.skipped.append(f"{reading_id}: {exc}")
            continue
        joules = conn.execute(
            "SELECT joules FROM gj_meter_reading WHERE reading_id = ?", (reading_id,)
        ).fetchone()["joules"]
        outcome = MintOutcome(
            minted=outcome.minted + 1,
            joules=outcome.joules + int(joules),
            skipped=outcome.skipped,
        )
    return outcome


def balance(conn: sqlite3.Connection, account_id: str) -> int:
    """Read a balance from the view, never from a stored column."""
    row = conn.execute(
        "SELECT joules FROM v_gj_balance WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise LedgerError(f"no such account: {account_id!r}")
    return int(row["joules"])


def conservation(conn: sqlite3.Connection) -> sqlite3.Row:
    """The invariant, as one row. `residual_joules` is 0 on a healthy ledger."""
    return conn.execute("SELECT * FROM v_gj_conservation").fetchone()
