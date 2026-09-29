"""The one authority database, and the only function that opens it.

Role: database access
Reads: runtime/state/gigajoule.db
Writes: runtime/state/gigajoule.db (schema application only; rows go through ledger.py)
Can move tokens: no
Live-safe: yes

ONE DATABASE IS THE AUTHORITY. `runtime/state/gigajoule.db` is the only place
any decision may be read from. Not a CSV, not a JSON export, not a cache, and
not a second database. Exports under `runtime/` are MIRRORS -- for other systems
to read, for operator visibility, for compatibility with an older consumer --
and nothing on the minting or transfer path may parse one. The test for whether
a file is a mirror or an authority is simple: if it went stale, truncated or
half-written, would anything decide differently? On the main path the answer
must be "no, because nothing reads it for that".

A second database is allowed only to keep a concurrent WRITER off this one's
lock -- SQLite has exactly one writer lock per database file -- and if one is
ever added, it gets exactly one reader, the justification goes in a comment at
its connect site, and no gate may read it. A staging file that a second consumer
starts reading has become a source of truth, and the two will disagree.

EVERY CONNECTION SETS `PRAGMA foreign_keys = ON`, AND THAT IS NOT OPTIONAL.
SQLite defaults it OFF and it is per-connection, not a property of the file. The
schema's vocabulary tables, its account and reading references are all foreign
keys, so a connection that forgets this pragma silently enforces none of them --
and it fails in the worst direction, by ACCEPTING a row that should have been
refused. `tests/ledger/test_schema.py` asserts a connection from this function
has it on.

ISOLATION IS MANAGED EXPLICITLY (`isolation_level=None`). Python's sqlite3
otherwise opens transactions implicitly on DML and commits at times that are
hard to reason about, and every write in this system is a multi-row batch that
must be all-or-nothing: half a mint is a token with no matching debit, which is
precisely the state the conservation view exists to prove impossible. Writers
say BEGIN IMMEDIATE and COMMIT themselves, where a reader can see them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gigajoule_core.schema import SCHEMA_SQL, SCHEMA_VERSION


class SchemaVersionError(RuntimeError):
    """The database on disk is not the shape this code expects."""


REPO_ROOT = Path(__file__).resolve().parent.parent
AUTHORITY_DB = REPO_ROOT / "runtime" / "state" / "gigajoule.db"

# Seconds, and named so. An external API's unit stays that API's unit: converting
# here to satisfy the reporting convention in cycle.py would put rounding into
# control flow. Timings are converted to microfortnights on the way OUT, at the
# print, never on the way in.
BUSY_TIMEOUT_SECONDS = 30.0


def connect(db_path: Path | None = None, *, create: bool = False) -> sqlite3.Connection:
    """Open the authority database with the settings every caller must have.

    `create=False` (the default) REFUSES to open a database that does not exist,
    rather than letting SQLite conjure an empty one. An empty ledger answers
    every balance query with zero and every conservation check with "healthy",
    so a typo in a path would present as a system holding nothing rather than as
    an error -- the failure shape where the caller cannot tell a mistake from a
    real answer.
    """
    path = AUTHORITY_DB if db_path is None else db_path
    if not create and not path.exists():
        raise FileNotFoundError(
            f"no authority database at {path}. Run `./gigajoule init` to create "
            "one. Refusing to open it implicitly, because an empty ledger reads "
            "as a healthy system holding nothing."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not create:
        _require_schema_version(conn, path)
    return conn


def _require_schema_version(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a database whose schema is not the one this code expects.

    OPENING IT ANYWAY IS THE FAILURE TO AVOID, and it is worse than it sounds. A
    query against an older schema does not raise where it matters -- it reads a
    column that is not there (which raises, loudly, fine) or a column whose
    MEANING changed (which does not). Between versions 1 and 2 of this schema,
    holdings went from per-account to per-lot: a version-1 database answered
    `v_gj_balance` correctly and had no idea what a lot was, so version-2 code
    reading it would find every position unattributed rather than find an error.

    There is no migration path here and that is deliberate for now: nothing has
    run in production, so the honest instruction is to delete and re-init rather
    than to write and test a migration nobody needs. The moment a real ledger
    exists that stops being true -- a ledger cannot be re-initialized, that is
    the entire point of it -- and this function is where the migration hook goes.
    """
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found == SCHEMA_VERSION:
        return
    conn.close()
    raise SchemaVersionError(
        f"{path} is schema version {found}; this code expects {SCHEMA_VERSION}.\n"
        "  Nothing has been read from it. Refusing to open a database whose columns "
        "may mean something different than this code assumes.\n"
        "  There is no migration yet. If this ledger holds nothing you need, delete "
        "it and run `./gigajoule init`. If it holds real positions, STOP: a ledger "
        "is not re-initializable, and a migration has to be written."
    )


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table, index, trigger and view. Idempotent.

    Safe to run against a populated database: every statement in SCHEMA_SQL is
    IF NOT EXISTS or INSERT OR IGNORE, and none touches a ledger row.
    """
    conn.executescript(SCHEMA_SQL)


def open_ledger(db_path: Path | None = None) -> sqlite3.Connection:
    """Create the database if needed, apply the schema, and return it ready."""
    conn = connect(db_path, create=True)
    apply_schema(conn)
    return conn
