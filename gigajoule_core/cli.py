"""The operator-facing commands. Everything a human runs enters here.

Role: command line interface
Reads: runtime/state/gigajoule.db
Writes: runtime/state/gigajoule.db, via ledger.py
Can move tokens: YES -- `mint`, `transfer` and `retire` write ledger rows
Live-safe: `status` and `verify` are read-only; the rest are not

SILENCE IS A DEFECT, AND THAT SHAPES EVERY PRINT IN THIS FILE.

An operator watching a blinking cursor cannot tell working from hung, and the
way that resolves is Ctrl-C -- which on a system mid-write means killing a
process partway through a batch. So:

  - Announce BEFORE, not only after. Every command prints its database path and
    the scale of what it is about to do before it starts. A line that appears
    only on completion is invisible during exactly the wait it was written for.
  - Never let an empty result print nothing. `(none)` is a result; a blank gap
    is ambiguous between zero rows and a query that failed.
  - Say what a number means, next to the number. The operator reads the screen,
    not this source.
  - Make "did nothing" look different from "did work". A pass that minted
    nothing must not share a success line with one that minted forty.
  - Echo the parameters that decide the answer, because pasted output is
    usually read a day later and has to be self-describing.

Timings print in microfortnights with the seconds in parentheses, per cycle.py,
so a figure can be matched against a stopwatch without arithmetic.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from gigajoule_core import ledger
from gigajoule_core.cycle import format_duration
from gigajoule_core.db import AUTHORITY_DB, SchemaVersionError, connect, open_ledger
from gigajoule_core.lots import InsufficientHoldings
from gigajoule_core.schema import ISSUANCE_ACCOUNT, RETIREMENT_ACCOUNT
from gigajoule_core.storage import StorageError, release_joules
from gigajoule_core.units import format_gj, kwh_to_joules


def _now() -> int:
    return int(time.time())


def _banner(db_path: Path, what: str) -> None:
    """What is about to happen, and where. Printed before the work, always."""
    print(f"gigajoule: {what}")
    print(f"  database   {db_path}")


def cmd_init(args: argparse.Namespace) -> int:
    started = time.monotonic()
    _banner(args.db, "creating the authority database and applying the schema")
    existed = args.db.exists()
    conn = open_ledger(args.db)
    ledger.bootstrap_system_accounts(conn, now=_now())
    objects = conn.execute(
        "SELECT type, COUNT(*) AS n FROM sqlite_master WHERE name LIKE 'gj_%' OR name LIKE 'v_gj_%'"
        " GROUP BY type ORDER BY type"
    ).fetchall()
    conn.close()
    print(f"  state      {'already existed, schema re-applied' if existed else 'created'}")
    for row in objects:
        print(f"  {row['type']:<10} {row['n']}")
    print(f"  done in {format_duration(time.monotonic() - started)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    started = time.monotonic()
    _banner(args.db, "reading balances and the conservation invariant (read-only)")
    conn = connect(args.db)
    row = ledger.conservation(conn)

    print("\n  supply")
    print(f"    minted       {format_gj(row['minted_joules']):>16}   all energy ever tokenized")
    print(f"    circulating  {format_gj(row['circulating_joules']):>16}   held by holders now")
    print(f"    stored       {format_gj(row['stored_joules']):>16}   sitting in batteries")
    print(f"    retired      {format_gj(row['retired_joules']):>16}   claimed against consumption")
    print(f"    lost         {format_gj(row['lost_joules']):>16}   destroyed by storage round-trip")
    # Read from the view rather than recomputed here. This line is why: the
    # arithmetic used to live at each reader, and this one went stale when
    # storage added two destinations.
    holds = row["supply_residual_joules"] == 0
    print(
        f"    {'identity holds' if holds else 'IDENTITY BROKEN':>16}"
        f"   minted == circulating + stored + retired + lost"
    )

    balances = conn.execute(
        "SELECT account_id, kind, joules, entries FROM v_gj_balance "
        "WHERE kind = 'HOLDER' AND entries > 0 ORDER BY joules DESC"
    ).fetchall()
    print("\n  holders")
    if not balances:
        print("    (none)  <- no holder has any ledger entry yet")
    for bal in balances:
        print(f"    {bal['account_id']:<24} {format_gj(bal['joules']):>16}  {bal['entries']} entries")

    queued = conn.execute("SELECT COUNT(*) AS n FROM v_gj_unminted_reading").fetchone()["n"]
    refused = conn.execute("SELECT COUNT(*) AS n FROM v_gj_refused_reading").fetchone()["n"]
    print("\n  readings")
    print(f"    queued to mint  {queued:>6}   <- expected 0 after a mint pass")
    print(f"    refused         {refused:>6}   <- non-zero means a broken input upstream")
    conn.close()
    print(f"\n  done in {format_duration(time.monotonic() - started)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Every invariant that can be read from a view. Read-only, safe on a live ledger."""
    started = time.monotonic()
    _banner(args.db, "checking every ledger invariant (read-only)")
    conn = connect(args.db)
    failures = 0

    row = ledger.conservation(conn)
    residual = row["residual_joules"]
    ok = residual == 0
    failures += 0 if ok else 1
    print(f"\n  conservation      {'PASS' if ok else 'FAIL'}   residual {residual}J (expected 0)")

    imbalanced = conn.execute("SELECT * FROM v_gj_imbalanced_batch").fetchall()
    failures += 0 if not imbalanced else 1
    print(
        f"  balanced batches  {'PASS' if not imbalanced else 'FAIL'}   "
        f"{len(imbalanced)} batch(es) whose legs do not sum to zero (expected 0)"
    )

    supply = row["supply_residual_joules"]
    failures += 0 if supply == 0 else 1
    print(
        f"  supply identity   {'PASS' if supply == 0 else 'FAIL'}   "
        f"minted - (circulating + stored + retired + lost) = {supply}J (expected 0)"
    )

    stray_lots = conn.execute("SELECT * FROM v_gj_imbalanced_lot").fetchall()
    failures += 0 if not stray_lots else 1
    print(
        f"  balanced lots     {'PASS' if not stray_lots else 'FAIL'}   "
        f"{len(stray_lots)} lot(s) whose entries do not sum to zero (expected 0)"
    )

    bridge = row["conversion_joules"]
    failures += 0 if bridge == 0 else 1
    print(
        f"  conversion bridge {'PASS' if bridge == 0 else 'FAIL'}   "
        f"CONVERSION holds {bridge}J (expected 0; non-zero means a half-written lot bridge)"
    )

    negative = conn.execute(
        "SELECT account_id, joules FROM v_gj_balance WHERE kind IN ('HOLDER','STORAGE') "
        "   AND joules < 0"
    ).fetchall()
    failures += 0 if not negative else 1
    print(
        f"  no overdrafts     {'PASS' if not negative else 'FAIL'}   "
        f"{len(negative)} holder(s) with a negative balance (expected 0)"
    )

    conn.close()
    print(f"\n  {failures} check(s) failed. done in {format_duration(time.monotonic() - started)}")
    return 1 if failures else 0


def cmd_source_add(args: argparse.Namespace) -> int:
    _banner(args.db, f"registering source {args.source_id}")
    conn = connect(args.db)
    ledger.register_source(
        conn,
        ledger.Source(
            source_id=args.source_id,
            fuel=args.fuel,
            grid_region=args.region,
            nameplate_watts=args.watts,
        ),
        now=_now(),
    )
    ceiling = args.watts * 3600
    print(f"  registered {args.source_id}: {args.fuel} in {args.region}, {args.watts}W nameplate")
    print(f"  physical ceiling {format_gj(ceiling)} per hour <- readings above this are refused")
    conn.close()
    return 0


def cmd_account_add(args: argparse.Namespace) -> int:
    _banner(args.db, f"opening account {args.account_id}")
    conn = connect(args.db)
    ledger.open_account(conn, args.account_id, args.name, now=_now())
    print(f"  opened {args.account_id} ({args.name}), balance {format_gj(0)}")
    conn.close()
    return 0


def cmd_reading_add(args: argparse.Namespace) -> int:
    _banner(args.db, f"recording reading {args.reading_id} for {args.source_id}")
    conn = connect(args.db)
    joules = kwh_to_joules(args.kwh)
    print(f"  {args.kwh}kWh -> {joules}J ({format_gj(joules)}), exact conversion")
    accepted = ledger.record_reading(
        conn,
        ledger.MeterReading(
            reading_id=args.reading_id,
            source_id=args.source_id,
            interval_start=args.start,
            interval_end=args.end,
            joules=joules,
            attestor=args.attestor,
        ),
        now=_now(),
    )
    if accepted:
        print("  ACCEPTED, queued to mint")
    else:
        detail = conn.execute(
            "SELECT detail FROM v_gj_refused_reading WHERE reading_id = ?", (args.reading_id,)
        ).fetchone()
        # `detail` already opens with REFUSED -- CeilingVerdict.reason() writes
        # it that way so the refusal reads the same wherever it is surfaced.
        # Printing a second prefix here produced "REFUSED and kept as evidence:
        # REFUSED: ..." on the first end-to-end run, which is the kind of thing
        # only running the command shows.
        print(f"  {detail['detail']}")
        print("  the reading is kept as evidence and left out of the mint queue")
    conn.close()
    return 0 if accepted else 2


def cmd_mint(args: argparse.Namespace) -> int:
    started = time.monotonic()
    _banner(args.db, f"minting every queued reading into {args.to}")
    conn = connect(args.db)
    queued = conn.execute("SELECT COUNT(*) AS n FROM v_gj_unminted_reading").fetchone()["n"]
    print(f"  {queued} reading(s) queued")
    outcome = ledger.mint_pending(conn, to_account=args.to, now=_now())

    if outcome.minted == 0 and not outcome.skipped:
        print("  NOTHING TO DO: the queue was empty, no tokens were created")
    else:
        print(f"  MINTED {outcome.minted} reading(s), {format_gj(outcome.joules)} to {args.to}")
    if outcome.skipped:
        print(f"  SKIPPED {len(outcome.skipped)}:")
        for line in outcome.skipped:
            print(f"    {line}")
    conn.close()
    print(f"  done in {format_duration(time.monotonic() - started)}")
    return 0


def cmd_transfer(args: argparse.Namespace) -> int:
    _banner(args.db, f"transferring {args.kwh}kWh from {args.sender} to {args.recipient}")
    conn = connect(args.db)
    joules = kwh_to_joules(args.kwh)
    batch = ledger.transfer(
        conn,
        ledger.Movement(
            from_account=args.sender, to_account=args.recipient, joules=joules, memo=args.memo
        ),
        now=_now(),
    )
    print(f"  moved {format_gj(joules)}, batch {batch}")
    print(f"  {args.sender:<20} {format_gj(ledger.balance(conn, args.sender)):>16}")
    print(f"  {args.recipient:<20} {format_gj(ledger.balance(conn, args.recipient)):>16}")
    conn.close()
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    _banner(args.db, f"retiring {args.kwh}kWh held by {args.account_id} -- THIS IS IRREVERSIBLE")
    conn = connect(args.db)
    joules = kwh_to_joules(args.kwh)
    batch = ledger.retire(
        conn,
        ledger.Claim(account_id=args.account_id, joules=joules, memo=args.memo),
        now=_now(),
    )
    print(f"  retired {format_gj(joules)}, batch {batch}")
    print(f"  {args.account_id} now holds {format_gj(ledger.balance(conn, args.account_id))}")
    print(f"  retired to date {format_gj(ledger.balance(conn, RETIREMENT_ACCOUNT))}")
    print(f"  issuance stands at {format_gj(ledger.balance(conn, ISSUANCE_ACCOUNT))} (minted total, negated)")
    conn.close()
    return 0


def cmd_battery_add(args: argparse.Namespace) -> int:
    _banner(args.db, f"registering storage asset {args.asset_id}")
    conn = connect(args.db)
    capacity = kwh_to_joules(args.capacity_kwh)
    ledger.register_storage_asset(
        conn,
        ledger.StorageAsset(
            asset_id=args.asset_id,
            account_id=f"storage:{args.asset_id}",
            grid_region=args.region,
            capacity_joules=capacity,
            round_trip_bp=args.efficiency_bp,
        ),
        now=_now(),
    )
    out, lost = release_joules(capacity, args.efficiency_bp)
    print(f"  registered {args.asset_id} in {args.region}")
    print(f"  capacity   {format_gj(capacity)}  ({args.capacity_kwh}kWh)")
    print(f"  efficiency {args.efficiency_bp}bp = {args.efficiency_bp / 100}%")
    print(f"  a full charge released now would deliver {format_gj(out)} and lose {format_gj(lost)}")
    conn.close()
    return 0


def cmd_charge(args: argparse.Namespace) -> int:
    _banner(args.db, f"charging {args.asset_id} with {args.kwh}kWh from {args.sender}")
    conn = connect(args.db)
    joules = kwh_to_joules(args.kwh)
    ledger.charge(
        conn,
        ledger.Charge(asset_id=args.asset_id, from_account=args.sender, joules=joules),
        now=_now(),
    )
    state = conn.execute(
        "SELECT * FROM v_gj_storage_state WHERE asset_id = ?", (args.asset_id,)
    ).fetchone()
    print(f"  moved {format_gj(joules)} into storage. Title now sits with the asset.")
    print(f"  charged  {format_gj(state['charged_joules'])} of {format_gj(state['capacity_joules'])}")
    print(f"  headroom {format_gj(state['headroom_joules'])}")
    print(f"  lots held {state['lots_held']}  <- provenance is preserved, not merged")
    conn.close()
    return 0


def cmd_discharge(args: argparse.Namespace) -> int:
    started = time.monotonic()
    _banner(args.db, f"discharging {args.kwh}kWh from {args.asset_id} to {args.to}")
    conn = connect(args.db)
    joules = kwh_to_joules(args.kwh)
    print(f"  drawing {format_gj(joules)} OUT OF STORAGE; physics decides what arrives")
    outcome = ledger.discharge(
        conn,
        ledger.Discharge(asset_id=args.asset_id, to_account=args.to, joules=joules),
        now=_now(),
    )
    print(f"  delivered {format_gj(outcome.released_joules)} to {args.to}")
    print(f"  lost      {format_gj(outcome.lost_joules)} to round-trip inefficiency")
    print(f"  new lots  {len(outcome.child_lots)}  <- one per source lot, vintage = release time")
    if not outcome.child_lots:
        print("  (none)  <- nothing was released; the whole drawdown was loss")
    conn.close()
    print(f"  done in {format_duration(time.monotonic() - started)}")
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    _banner(args.db, f"listing lots held by {args.account_id} (read-only)")
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT * FROM v_gj_holding WHERE account_id = ? ORDER BY vintage_start", (args.account_id,)
    ).fetchall()
    if not rows:
        print("  (none)  <- this account holds no lots")
    for row in rows:
        print(
            f"  {format_gj(row['joules']):>14}  {row['fuel']:<8} {row['grid_region']:<8} "
            f"{row['origin']:<16} {row['vintage_start_utc']} -> {row['vintage_end_utc']}"
        )
    print(f"  {len(rows)} lot(s), {format_gj(sum(r['joules'] for r in rows))} total")
    conn.close()
    return 0


def _add_registry_commands(sub: argparse._SubParsersAction) -> None:
    """Commands that describe the physical world: sources, accounts, readings."""
    p = sub.add_parser("source-add", help="register a metered generation asset")
    p.add_argument("source_id")
    p.add_argument("--fuel", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--watts", type=int, required=True, help="nameplate rating, watts")
    p.set_defaults(func=cmd_source_add)

    p = sub.add_parser("account-add", help="open a holder account")
    p.add_argument("account_id")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_account_add)

    p = sub.add_parser("reading-add", help="submit one interval reading")
    p.add_argument("reading_id")
    p.add_argument("--source-id", required=True, dest="source_id")
    p.add_argument("--kwh", required=True, help="energy in kWh, as text (never a float)")
    p.add_argument("--start", type=int, required=True, help="interval start, epoch seconds UTC")
    p.add_argument("--end", type=int, required=True, help="interval end, epoch seconds UTC")
    p.add_argument("--attestor", required=True)
    p.set_defaults(func=cmd_reading_add)

    p = sub.add_parser("mint", help="mint every queued reading")
    p.add_argument("--to", required=True, help="account to credit")
    p.set_defaults(func=cmd_mint)

    p = sub.add_parser("transfer", help="move tokens between holders")
    p.add_argument("sender")
    p.add_argument("recipient")
    p.add_argument("--kwh", required=True)
    p.add_argument("--memo", default="")
    p.set_defaults(func=cmd_transfer)


def _add_storage_commands(sub: argparse._SubParsersAction) -> None:
    """Commands for batteries. Split out because storage is the one place an
    operation is not conservative -- discharging destroys joules -- and grouping
    them makes that boundary visible in the help text as well as in the code."""
    p = sub.add_parser("battery-add", help="register a storage asset")
    p.add_argument("asset_id")
    p.add_argument("--region", required=True)
    p.add_argument("--capacity-kwh", required=True, dest="capacity_kwh")
    p.add_argument(
        "--efficiency-bp",
        type=int,
        default=8800,
        dest="efficiency_bp",
        help="round-trip efficiency in basis points; 8800 = 88.00%% (default)",
    )
    p.set_defaults(func=cmd_battery_add)

    p = sub.add_parser("charge", help="sell energy into a battery")
    p.add_argument("asset_id")
    p.add_argument("--from", required=True, dest="sender")
    p.add_argument("--kwh", required=True)
    p.set_defaults(func=cmd_charge)

    p = sub.add_parser("discharge", help="release energy from a battery (loses some to heat)")
    p.add_argument("asset_id")
    p.add_argument("--to", required=True)
    p.add_argument("--kwh", required=True, help="energy drawn OUT OF STORAGE, not delivered")
    p.set_defaults(func=cmd_discharge)

    p = sub.add_parser("holdings", help="list the lots an account holds (read-only)")
    p.add_argument("account_id")
    p.set_defaults(func=cmd_holdings)


def _add_ledger_commands(sub: argparse._SubParsersAction) -> None:
    """Commands that move tokens between accounts."""
    p = sub.add_parser("retire", help="claim energy against consumption (irreversible)")
    p.add_argument("account_id")
    p.add_argument("--kwh", required=True)
    p.add_argument("--memo", default="")
    p.set_defaults(func=cmd_retire)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gigajoule", description="Tokenization of electricity: the operator interface."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=AUTHORITY_DB,
        help=f"authority database (default: {AUTHORITY_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and apply the schema").set_defaults(
        func=cmd_init
    )
    sub.add_parser("status", help="balances and supply (read-only)").set_defaults(func=cmd_status)
    sub.add_parser("verify", help="check every invariant (read-only)").set_defaults(func=cmd_verify)
    _add_registry_commands(sub)
    _add_storage_commands(sub)
    _add_ledger_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        ledger.LedgerError,
        sqlite3.IntegrityError,
        FileNotFoundError,
        InsufficientHoldings,
        SchemaVersionError,
        StorageError,
    ) as exc:
        # Refusals are the ordinary outcome here, not crashes, so they print as
        # one legible line rather than a traceback an operator has to read past.
        print(f"\n  REFUSED: {exc}")
        return 2
