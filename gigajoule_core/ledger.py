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
from gigajoule_core.lots import ANY_LOT, Holding, LotSpec, select_lots
from gigajoule_core.schema import (
    CONVERSION_ACCOUNT,
    ISSUANCE_ACCOUNT,
    LOSS_ACCOUNT,
    RETIREMENT_ACCOUNT,
    SYSTEM_ACCOUNTS,
)
from gigajoule_core.storage import check_efficiency, release_joules


class LedgerError(RuntimeError):
    """A refused ledger operation.

    Raised rather than returning a falsy value because every caller is about to
    report what happened, and a bare None would leave "nothing to do" and "this
    was refused" indistinguishable in the output -- the failure shape where a
    skipped operation and a completed one read the same on a status screen.
    """


@dataclass(frozen=True)
class Leg:
    """One side of a balanced batch. Positive credits, negative debits.

    `lot_id` is required on every leg. Joules without a lot are joules without
    provenance, which is the one thing this system exists not to have.
    """

    account_id: str
    joules_delta: int
    lot_id: str


@dataclass(frozen=True)
class NewLot:
    """A lot to create as part of a batch, in the same transaction.

    Created INSIDE `_post_batch`'s transaction rather than before it, so a crash
    between the two cannot leave a lot with no entries. Such an orphan is
    harmless to every invariant here -- it has no rows, so it cannot imbalance
    anything -- and it is still a row claiming energy exists that nobody ever
    issued, which is not a thing this registry should be able to accumulate.
    """

    lot_id: str
    origin: str
    fuel: str
    grid_region: str
    vintage_start: int
    vintage_end: int
    reading_id: str | None = None
    parent_lot_id: str | None = None


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
    new_lots: list[NewLot] = field(default_factory=list)

    def residual(self) -> int:
        """Joules unaccounted for. Zero on a well-formed batch."""
        return sum(leg.joules_delta for leg in self.legs)

    def lot_residuals(self) -> dict[str, int]:
        """Per-lot residual. Every value is zero on a well-formed batch.

        Checked separately from `residual()` because they catch different bugs. A
        batch that debits one lot and credits another sums to zero globally and
        has still moved energy between provenances without saying so -- which is
        the whole failure mode `v_gj_imbalanced_lot` exists to detect, caught
        here before the rows are written rather than after.
        """
        totals: dict[str, int] = {}
        for leg in self.legs:
            totals[leg.lot_id] = totals.get(leg.lot_id, 0) + leg.joules_delta
        return totals


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
    """Tokens moving from one account to another.

    `spec` is what the RECIPIENT insists on -- solar only, this region only, this
    delivery window only. It filters which of the sender's lots are eligible, and
    a transfer that cannot be filled from eligible lots is refused rather than
    filled with something else.
    """

    from_account: str
    to_account: str
    joules: int
    memo: str = ""
    spec: LotSpec = ANY_LOT


@dataclass(frozen=True)
class Claim:
    """A retirement: energy claimed against consumption, with what it must be.

    `spec` carries more weight here than anywhere else. Retirement is where the
    claim is actually made -- "this consumption was covered by solar generated in
    this hour" -- so the lots retired ARE the evidence. Retiring against an
    unfiltered spec claims only that some energy, somewhere, once existed.
    """

    account_id: str
    joules: int
    memo: str = ""
    spec: LotSpec = ANY_LOT


@dataclass(frozen=True)
class StorageAsset:
    """A battery, as registered. Efficiency in integer basis points: 8800 = 88%."""

    asset_id: str
    account_id: str
    grid_region: str
    capacity_joules: int
    round_trip_bp: int


@dataclass(frozen=True)
class Charge:
    """Energy sold into a battery. Ordinary transfer of title; lots unchanged."""

    asset_id: str
    from_account: str
    joules: int
    memo: str = ""
    spec: LotSpec = ANY_LOT


@dataclass(frozen=True)
class Discharge:
    """Energy released from a battery to a recipient.

    `joules` is the amount taken OUT OF STORAGE, not the amount delivered. Those
    differ by the round-trip loss and conflating them is the obvious unit error
    here, so the field is named for the side the operator controls: you decide
    how much to draw down the battery, physics decides how much arrives.
    `DischargeOutcome` reports both.
    """

    asset_id: str
    to_account: str
    joules: int
    memo: str = ""
    spec: LotSpec = ANY_LOT
    # The window the released energy is delivered over, which becomes the new
    # lot's vintage. One hour by default because that is the settlement
    # granularity most markets clear at; a real integration should pass the
    # actual window rather than inherit this, since the vintage IS the product
    # for anyone doing time-matched accounting.
    release_window_seconds: int = 3600


@dataclass(frozen=True)
class DischargeOutcome:
    """What a discharge actually did. Both sides of the loss, never just one."""

    batch_id: str
    drawn_joules: int
    released_joules: int
    lost_joules: int
    child_lots: list[str] = field(default_factory=list)


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
    unbalanced_lots = {lot: n for lot, n in batch.lot_residuals().items() if n != 0}
    if unbalanced_lots:
        raise LedgerError(
            f"batch balances overall but not per lot: {unbalanced_lots}. Energy has "
            "moved between provenances without passing through CONVERSION, which "
            "means joules changed their fuel, region or vintage without a record "
            "of the transformation."
        )
    batch_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO gj_lot "
            "(lot_id, origin, reading_id, parent_lot_id, fuel, grid_region, "
            " vintage_start, vintage_end, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lot.lot_id,
                    lot.origin,
                    lot.reading_id,
                    lot.parent_lot_id,
                    lot.fuel,
                    lot.grid_region,
                    lot.vintage_start,
                    lot.vintage_end,
                    now,
                )
                for lot in batch.new_lots
            ],
        )
        conn.executemany(
            "INSERT INTO gj_ledger_entry "
            "(batch_id, account_id, joules_delta, reason, lot_id, reading_id, memo, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    leg.account_id,
                    leg.joules_delta,
                    batch.reason,
                    leg.lot_id,
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
    conservation check a single SELECT. The list is `schema.SYSTEM_ACCOUNTS`
    rather than four literals here, so adding a fifth cannot mean editing two
    places and remembering both.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO gj_account (account_id, kind, display_name, opened_at) "
        "VALUES (?, ?, ?, ?)",
        [(account_id, kind, name, now) for account_id, kind, name in SYSTEM_ACCOUNTS],
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


def holdings_of(
    conn: sqlite3.Connection, account_id: str, spec: LotSpec = ANY_LOT
) -> list[Holding]:
    """What an account holds, as lot records the selector can reason about.

    Reads `v_gj_holding`, which already excludes spent-to-zero positions, and
    hands back plain values. The FILTERING is done by `lots.eligible()` in Python
    rather than by a WHERE clause built here, so that one function decides
    eligibility for every caller -- a spec applied one way in SQL and another way
    in the selector is two rules that agree until somebody edits one.
    """
    rows = conn.execute(
        "SELECT lot_id, joules, vintage_start, vintage_end, fuel, grid_region "
        "  FROM v_gj_holding WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    held = [
        Holding(
            lot_id=row["lot_id"],
            joules=int(row["joules"]),
            vintage_start=int(row["vintage_start"]),
            vintage_end=int(row["vintage_end"]),
            fuel=row["fuel"],
            grid_region=row["grid_region"],
        )
        for row in rows
    ]
    return [h for h in held if spec.matches(h)]


def register_storage_asset(conn: sqlite3.Connection, asset: StorageAsset, *, now: int) -> None:
    """Register a battery and open the account that holds its energy.

    The efficiency is checked here rather than at discharge because it is written
    once and read forever, so the cheap place to catch a mistyped one is where it
    enters. `check_efficiency` refuses a value that is almost certainly a
    fraction entered where basis points were meant.
    """
    check_efficiency(asset.round_trip_bp)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO gj_account (account_id, kind, display_name, opened_at) "
            "VALUES (?, 'STORAGE', ?, ?)",
            (asset.account_id, f"Storage asset {asset.asset_id}", now),
        )
        conn.execute(
            "INSERT INTO gj_storage_asset "
            "(asset_id, account_id, grid_region, capacity_joules, round_trip_bp, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                asset.asset_id,
                asset.account_id,
                asset.grid_region,
                asset.capacity_joules,
                asset.round_trip_bp,
                now,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mint(conn: sqlite3.Connection, reading_id: str, *, to_account: str, now: int) -> str:
    """Turn one verified reading into a lot of tokens.

    Creates the lot and posts the batch in one transaction. The lot's vintage is
    the meter interval -- when the energy was actually generated -- and its fuel
    and region are copied from the source rather than referenced, because a lot
    has to carry its own provenance: a storage release has no reading and still
    has a fuel, so the attribute cannot live only on the reading.
    """
    row = conn.execute(
        "SELECT r.joules, r.interval_start, r.interval_end, s.fuel, s.grid_region, "
        "       EXISTS(SELECT 1 FROM gj_reading_refusal f WHERE f.reading_id = r.reading_id) "
        "         AS refused, "
        "       EXISTS(SELECT 1 FROM gj_ledger_entry e "
        "               WHERE e.reading_id = r.reading_id AND e.reason = 'MINT') AS minted "
        "  FROM gj_meter_reading r "
        "  JOIN gj_source s ON s.source_id = r.source_id "
        " WHERE r.reading_id = ?",
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
    lot = NewLot(
        lot_id=uuid.uuid4().hex,
        origin="GENERATION",
        fuel=row["fuel"],
        grid_region=row["grid_region"],
        vintage_start=int(row["interval_start"]),
        vintage_end=int(row["interval_end"]),
        reading_id=reading_id,
    )
    return _post_batch(
        conn,
        Batch(
            legs=[
                Leg(ISSUANCE_ACCOUNT, -joules, lot.lot_id),
                Leg(to_account, joules, lot.lot_id),
            ],
            reason="MINT",
            reading_id=reading_id,
            memo=f"mint from reading {reading_id}",
            new_lots=[lot],
        ),
        now=now,
    )


def _movement_legs(conn: sqlite3.Connection, movement: Movement, *, reason: str) -> list[Leg]:
    """Turn "move N joules" into the per-lot legs that actually do it.

    One function for transfer, retirement and charging, because all three are the
    same operation with a different destination, and three copies of lot
    selection would be three chances to select differently.
    """
    if movement.joules <= 0:
        raise LedgerError(
            f"{reason.lower()} amount must be positive, got {movement.joules}J. A "
            "negative movement is a movement in the other direction and must be "
            "written as one, so the direction is legible in the ledger rather than "
            "in a sign."
        )
    picked = select_lots(
        holdings_of(conn, movement.from_account), movement.joules, movement.spec
    )
    legs: list[Leg] = []
    for lot_id, amount in picked:
        legs.append(Leg(movement.from_account, -amount, lot_id))
        legs.append(Leg(movement.to_account, amount, lot_id))
    return legs


def transfer(conn: sqlite3.Connection, movement: Movement, *, now: int) -> str:
    """Move tokens between holders. Changes no total and no provenance.

    Spends lots oldest-vintage-first by default -- see `lots.select_lots` for why
    that rule and not another. A transfer touches as many lots as it needs to
    cover the amount, all in ONE batch, because it is one economic event and
    splitting it would let an interruption deliver part of a payment.
    """
    if movement.from_account == movement.to_account:
        raise LedgerError(
            f"refusing a self-transfer on {movement.from_account!r}: it would write "
            "legs that change no balance, and every later reader would have to work "
            "out that it meant nothing"
        )
    legs = _movement_legs(conn, movement, reason="TRANSFER")
    return _post_batch(conn, Batch(legs=legs, reason="TRANSFER", memo=movement.memo), now=now)


def retire(conn: sqlite3.Connection, claim: Claim, *, now: int) -> str:
    """Claim energy against consumption. Irreversible by design.

    `spec` matters more here than anywhere else: retirement is where a claim is
    actually made -- "this consumption was covered by solar generated in this
    hour" -- so the lots retired are the evidence for it. Retiring against an
    unfiltered spec claims only that some energy existed.
    """
    legs = _movement_legs(
        conn,
        Movement(
            from_account=claim.account_id,
            to_account=RETIREMENT_ACCOUNT,
            joules=claim.joules,
            memo=claim.memo,
            spec=claim.spec,
        ),
        reason="RETIRE",
    )
    return _post_batch(conn, Batch(legs=legs, reason="RETIRE", memo=claim.memo), now=now)


def charge(conn: sqlite3.Connection, request: Charge, *, now: int) -> str:
    """Sell energy into a battery. Title moves; the lots are untouched.

    Nothing has happened to the energy yet, so nothing happens to its provenance.
    The loss is realized on the way out, not on the way in -- see storage.py for
    why the whole round trip is charged at discharge.

    Capacity is enforced by a trigger rather than checked here, so that it holds
    for a caller that never imports this module.
    """
    asset = _read_asset(conn, request.asset_id)
    legs = _movement_legs(
        conn,
        Movement(
            from_account=request.from_account,
            to_account=asset.account_id,
            joules=request.joules,
            memo=request.memo,
            spec=request.spec,
        ),
        reason="CHARGE",
    )
    return _post_batch(conn, Batch(legs=legs, reason="CHARGE", memo=request.memo), now=now)


def discharge(conn: sqlite3.Connection, request: Discharge, *, now: int) -> DischargeOutcome:
    """Release energy from a battery, moving it in TIME and losing some to heat.

    THIS IS THE ONLY OPERATION THAT CREATES A LOT WITHOUT A METER READING, and
    everything about its shape follows from making that safe.

    Per parent lot drawn down, it writes five legs:

        battery      -stored    in the parent lot   (the energy leaves storage)
        LOSS         +lost      in the parent lot   (what physics took)
        CONVERSION   +released  in the parent lot   (bridge out of the old lot)
        CONVERSION   -released  in the child lot    (bridge into the new lot)
        recipient    +released  in the child lot    (what actually arrives)

    The parent lot sums to zero, the child lot sums to zero, and the batch sums
    to zero -- all three, which is what lets `v_gj_imbalanced_lot` stay empty
    while energy legitimately crosses between provenances. Without the CONVERSION
    bridge the batch would still balance globally and both lots would be broken,
    which is precisely the bug that check exists to catch.

    ONE CHILD LOT PER PARENT LOT, never one merged lot for the discharge. A
    battery holding solar and wind releases some of each, and a single child lot
    would have to pick one fuel and be wrong about the rest -- laundering the
    attribute through the battery, which is the exact abuse a registry with
    provenance is built to prevent.

    THE CHILD'S VINTAGE IS THE RELEASE WINDOW, not the parent's generation
    window. That is the entire reason a battery is interesting here rather than
    being a warehouse: energy generated at noon and delivered at 8pm is a
    different product, and saying otherwise is the time-shifting misstatement
    that 24/7 carbon-free accounting exists to prevent.
    """
    asset = _read_asset(conn, request.asset_id)
    if request.joules <= 0:
        raise LedgerError(f"discharge amount must be positive, got {request.joules}J")
    if request.release_window_seconds <= 0:
        raise LedgerError(
            f"release window must be positive, got {request.release_window_seconds}s; "
            "it becomes the new lot's vintage and a lot with no duration cannot exist"
        )

    picked = select_lots(
        holdings_of(conn, asset.account_id), request.joules, request.spec
    )
    parents = {
        h.lot_id: h for h in holdings_of(conn, asset.account_id)
    }

    legs: list[Leg] = []
    new_lots: list[NewLot] = []
    released_total = 0
    lost_total = 0

    for parent_lot_id, stored in picked:
        parent = parents[parent_lot_id]
        released, lost = release_joules(stored, asset.round_trip_bp)
        released_total += released
        lost_total += lost

        legs.append(Leg(asset.account_id, -stored, parent_lot_id))
        # A zero-valued leg is refused by the schema (CHECK joules_delta <> 0),
        # so both of these are conditional. `lost == 0` happens only at 100%
        # efficiency, which no real asset has but which the type system allows;
        # `released == 0` happens when a drawdown is smaller than the rounding
        # step, and then the whole parcel is loss and no child lot exists.
        if lost:
            legs.append(Leg(LOSS_ACCOUNT, lost, parent_lot_id))
        if not released:
            continue

        child = NewLot(
            lot_id=uuid.uuid4().hex,
            origin="STORAGE_RELEASE",
            fuel=parent.fuel,
            grid_region=parent.grid_region,
            vintage_start=now,
            vintage_end=now + request.release_window_seconds,
            parent_lot_id=parent_lot_id,
        )
        new_lots.append(child)
        legs.append(Leg(CONVERSION_ACCOUNT, released, parent_lot_id))
        legs.append(Leg(CONVERSION_ACCOUNT, -released, child.lot_id))
        legs.append(Leg(request.to_account, released, child.lot_id))

    batch_id = _post_batch(
        conn,
        Batch(
            legs=legs,
            reason="DISCHARGE",
            memo=request.memo or f"discharge from {request.asset_id}",
            new_lots=new_lots,
        ),
        now=now,
    )
    return DischargeOutcome(
        batch_id=batch_id,
        drawn_joules=request.joules,
        released_joules=released_total,
        lost_joules=lost_total,
        child_lots=[lot.lot_id for lot in new_lots],
    )


def _read_asset(conn: sqlite3.Connection, asset_id: str) -> StorageAsset:
    row = conn.execute(
        "SELECT asset_id, account_id, grid_region, capacity_joules, round_trip_bp "
        "  FROM gj_storage_asset WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"no such storage asset: {asset_id!r}")
    return StorageAsset(
        asset_id=row["asset_id"],
        account_id=row["account_id"],
        grid_region=row["grid_region"],
        capacity_joules=int(row["capacity_joules"]),
        round_trip_bp=int(row["round_trip_bp"]),
    )


def reverse(conn: sqlite3.Connection, batch_id: str, *, now: int, memo: str) -> str:
    """Undo a batch with an equal and opposite one. Never an edit.

    The ledger is append-only, so a correction ADDS history rather than
    restating it: both the error and its reversal stay visible, which is what
    lets an auditor see that a mistake was made and corrected rather than
    finding a ledger that has simply always been right.

    Each reversing leg carries the SAME lot as the leg it undoes, so the reversal
    balances per-lot as well as overall -- which is the check that would catch a
    reversal that put the joules back into the wrong provenance.
    """
    legs = conn.execute(
        "SELECT account_id, joules_delta, reason, lot_id FROM gj_ledger_entry "
        " WHERE batch_id = ? ORDER BY entry_id",
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
    if any(leg["reason"] == "DISCHARGE" for leg in legs):
        raise LedgerError(
            f"batch {batch_id!r} is a discharge and cannot be reversed. Reversing it "
            "would put energy back into a battery that has already delivered it and "
            "would un-lose joules that physics actually destroyed -- the loss is not "
            "a bookkeeping entry, it left as heat. Correct a wrong recipient with a "
            "transfer back from them instead."
        )
    return _post_batch(
        conn,
        Batch(
            legs=[
                Leg(leg["account_id"], -int(leg["joules_delta"]), leg["lot_id"]) for leg in legs
            ],
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
