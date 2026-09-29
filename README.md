# Project Gigajoule

**Tokenization of electricity.** A token represents a quantity of energy that
was physically generated, by a known source, over a known interval, attested by
someone — and it can be transferred and must be retired exactly once.

The ledger unit is the **integer joule**. The display unit is the gigajoule.
Nothing is ever stored as a float.

---

## Quick start

```bash
git clone <this repo>
cd Project_Gigajoule
python3 -m pip install -e '.[dev]'     # pytest + ruff; the package itself has no dependencies

./gigajoule init
./gigajoule source-add solar-01 --fuel solar --region ERCOT --watts 1000
./gigajoule account-add acme --name "Acme Power"

# One hour of generation from a 1kW source. Exactly 1kWh, exactly 3,600,000J.
./gigajoule reading-add r1 --source-id solar-01 --kwh 1 \
    --start 1800000000 --end 1800003600 --attestor meter-co

./gigajoule mint --to acme
./gigajoule status
./gigajoule verify
```

`status` and `verify` are read-only and safe against a live ledger. Everything
else writes.

### What a refusal looks like

Submit 1 MWh from a 1 kW source over one hour — the classic Wh/kWh unit error —
and the ceiling check refuses it and says which failure it is:

```
$ ./gigajoule reading-add bad --source-id solar-01 --kwh 1000 \
      --start 1800003600 --end 1800007200 --attestor meter-co

  1000kWh -> 3600000000J (3.600000GJ), exact conversion
  REFUSED: 3600000000J exceeds the physical ceiling of 3600000J by 1000.000x.
  ~1x means a clipping inverter or a low nameplate; ~1000x means a Wh/kWh unit
  error upstream; anything else, check the registered nameplate.
  the reading is kept as evidence and left out of the mint queue
```

The reading is **stored anyway**. A meter reporting something impossible is the
evidence an investigation needs; what the refusal does is keep it out of the
mint queue, so a broken input never looks like a backlog.

---

## The three invariants

Everything else is arrangement. These are the product.

| Invariant | Where it lives | How to check it |
|---|---|---|
| **Conservation** — minted == held + retired, exactly | `v_gj_conservation` | `./gigajoule verify` |
| **Append-only** — nothing is edited or deleted, ever | triggers on `gj_ledger_entry`, `gj_meter_reading`, `gj_reading_refusal` | `tests/ledger/test_schema.py` |
| **One interval mints once** | `ux_gj_one_mint_per_reading`, plus a `UNIQUE` on the reading's own interval | `tests/ledger/test_mint.py` |
| **Every lot sums to zero too** | `v_gj_imbalanced_lot` | `tests/ledger/test_battery.py` |
| **Nobody spends a lot they don't hold** | `gj_no_overdraft`, per (account, lot) | `tests/ledger/test_schema.py` |

Conservation is a single `SELECT` rather than a reconciliation job because
minting is **double-entry**: it debits a system `ISSUANCE` account and credits
the holder, so every batch sums to zero and therefore the whole ledger does.
Two quantities fall out of the same table for free — issuance's balance is minus
the total ever minted, retirement's is the total ever retired.

Retiring moves tokens to an account they can never leave. It is not a delete:
the record of what was claimed is the evidence it was claimed once, and
double-claiming is the fraud this market has to make *checkable* rather than
merely forbidden.

---

## Layout

```
gigajoule                  entry point — dispatch only, at the root where it is findable
gigajoule_core/
  units.py                 integer joules, and why not floats or kWh
  attestation.py           the physical ceiling: energy = power x time
  schema.py                THE AUTHORITY — every rule, as SQL
  db.py                    the one function that opens the one database
  ledger.py                mint / transfer / retire / reverse
  cli.py                   the operator commands
  cycle.py                 microfortnight timing
  american_english.py      \
  module_header.py          |  code hygiene, all clean gates, no baselines
  repo_tree.py              |
  source_text.py           /
tests/
  ledger/                  seeded rows, real functions, assertions on real views
  units/
  code_hygiene/
runtime/state/             the authority database. Never committed.
CLAUDE.md                  the working rules. Read this before changing anything.
```

**The reasoning lives in the module docstrings, not in a design document.** Each
file explains why it is the way it is, at length, next to the code it describes
— which is the one place an explanation cannot drift away from what it
describes. If you want to know why the ledger stores joules, read the top of
`units.py`; why balances are a view, the top of `schema.py`; why the tests write
raw SQL, the top of `tests/ledger/test_schema.py`.

---

## Development

```bash
python3 -m ruff check .          # the declared standard; clean, no suppressions but two
python3 -m pytest                # 78 tests
```

Both are expected to pass with zero findings. There are no baseline files and no
tolerated backlog: every hygiene check is a clean gate, because on the day this
repository was created nothing violated one. See `CLAUDE.md` rule 19.

The suppression census is **two**, both on the one `subprocess.run` that asks
git what it tracks, and it is pinned by value in
`tests/code_hygiene/test_suppressions.py`. A third needs an argument written
into that module's docstring — not an entry in a baseline file.

---

## What is deliberately not built yet

Named here rather than left for someone to discover missing:

- **Futures, and the whole obligation layer.** A forward contract is a promise
  about energy that does not exist yet, so it must NOT be a token: minting one
  would mean `minted` no longer says "energy that exists" and the conservation
  check becomes a lie. Obligations are the next layer — a separate table,
  settled by delivering lots that match a spec — and allocation is the same
  object with a distribution rule.
- **No settlement or pricing.** The ledger records what exists and who holds it.
  What a gigajoule is worth is a separate problem.
- **No external attestation.** `attestor` is a string. Signature verification,
  registry integration and meter-side attestation are the obvious next layer and
  none of it exists.
- **No cadence vocabulary yet.** `CLAUDE.md` rule 11 states how interval cadence
  must be handled when it arrives; today every reading carries its own explicit
  interval and nothing normalizes across them.
- **No concurrency beyond SQLite's.** One writer lock, one process. Rule 15
  covers what to do when that stops being enough.
