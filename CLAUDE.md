# Working rules for Project Gigajoule

Standing instructions from the operator. They apply to every change, not just
the one that prompted them.

These standards were carried over from Project Mammon on 2026-09-29, at the
operator's instruction, with that repo's measurements and incident history left
behind. The REASONING is what transfers; the numbers belonged to a different
tree and would have been stale here on arrival. Where a rule wants a worked
example, the examples below are ones that happened in THIS repository and are
dated accordingly.

---

## 0. What this system is, and what makes it different from a database

Gigajoule tokenizes electricity. A token represents a quantity of energy that
was physically generated, by a known source, over a known interval, attested by
someone, and it can be transferred and must be retired exactly once.

Three properties make this a ledger rather than a table of numbers, and every
rule below is downstream of one of them:

- **Conservation is exact.** Joules minted equal joules held plus joules
  retired, with no residual and no tolerance, forever. `v_gj_conservation`
  returns 0 on a healthy ledger and the defect itself on a broken one.
- **History is append-only.** Nothing is edited or deleted. A correction is a
  REVERSAL batch that cites the original, so an auditor sees that a mistake was
  made and fixed rather than a ledger that has simply always been right.
- **Provenance is the product.** A buyer is paying for the claim about what
  produced the energy. A token whose source, fuel, region and interval cannot
  be traced is worth nothing, whatever the balance says.

The unit is the **integer joule**. Not the kilowatt-hour, not the gigajoule the
project is named for, and never a float of either. `gigajoule_core/units.py`
carries the full argument; the short version is that "exactly" is only
available in integer arithmetic, and a conservation check written with a
tolerance has a hole in it the width of the discrepancy nobody investigates.

---

## 1. Be verbose in comments and docstrings

Write the reasoning down, at length. When a piece of code exists because
something went wrong, the comment names the date, the measured numbers, and
what the wrong behavior looked like from the outside. A future reader needs to
know why the obvious simpler version is wrong, and the only place that survives
is next to the code.

Every module carries a header stating its role, what it reads, what it writes,
whether it can move tokens, and whether it is safe to run against a live
ledger:

```
Role: ledger operations
Reads: gj_meter_reading, gj_account
Writes: gj_ledger_entry
Can move tokens: YES
Live-safe: no -- every function here changes what exists
```

`tests/code_hygiene/test_headers.py` enforces the shape over every Python file
in the tree, including the extensionless `./gigajoule` entry point -- which is
the file an operator actually runs and therefore the one that must not be able
to dodge the check.

**It is a clean gate and it has no baseline file.** Every file complies today,
so there is nothing to tolerate. See rule 19.

The two fields that earn their place are the last two. `Can move tokens` is the
capability declaration: a module that writes `gj_ledger_entry` changes what
someone owns. `Live-safe` is the question an operator has to answer before
running anything at all, and it is the field most likely to be left off -- which
is why the check counts FIELDS rather than looking for the presence of a header.
A partial header is the worse defect, because it reads as complete at a glance
and nobody looks further.

Verbosity is never the thing to trim. Remove a comment only when deleting the
code it describes.

## 2. Remove dead code and unused files

Delete it, do not quarantine it. Git history is the archive; a file kept "for
reference" is a file the next person has to read, grep past, and reason about.
Two implementations of one thing is worse than either alone, because "which one
runs?" has to be answered before any other question can be.

Before deleting, prove it is dead rather than assuming: grep the whole tree for
the FILENAME, not just the import graph. Shell scripts, launchers, kill
patterns, cron entries and docs all reference files by name in ways an
import-based check never sees. When something dies, its test dies with it or
changes to pin the stronger invariant.

"I could not find a caller" is not "there is no caller". Say which one you
established.

## 3. Always be optimizing

Leave the thing faster, smaller, or clearer than it was. Measure before
claiming an improvement, and **state the denominator** -- a count without what
it was counted out of is not a measurement. If a number cannot be measured, say
that instead of estimating and letting the estimate harden into a fact.

`gigajoule_core/repo_tree.py` exists for exactly this: it reports whether its
walk came from the git index or a filesystem crawl, so any figure derived from
it can state its method alongside its number.

## 4. Prefer merging

Merge. Do not rebase, reset, or force-push shared history without being asked.
A fast-forward merge recovers a stale checkout with nothing lost; a reset
discards work that was never anywhere else.

## 5. SQL and Python are the main path

Gigajoule is a Python and SQL system. Execution, authority and state belong in
those two. Anything under `runtime/` that is not the authority database is a
**mirror** -- an export for something else to read, for operator visibility,
for compatibility. Mirrors are never the authority for a live decision, and
nothing on the mint, transfer or retire path may depend on parsing one.

Use as much SQL as is feasible. A rule expressed as a constraint, a trigger or
a view answers the same way for every reader: an auditor at a sqlite3 prompt, a
future service in another language, a test seeding rows directly, and the
application itself. The same rule expressed as a Python check answers only for
whoever called that function, and only if they called it.

The practical tests, when adding or changing a stage:

- **Where does the decision live?** If a reader has to open a file to learn
  whether something is authorized, the authority is in the wrong place.
- **What happens if it is stale, truncated or half-written?** On the main path
  the answer must be "nothing, because nothing reads it for that".
- **Could this be a view?** If the logic is a filter, a join or a ranking over
  rows already in `gigajoule.db`, it probably should be. Balances ARE a view
  here (`v_gj_balance`), which is what makes a stale balance column impossible
  rather than merely discouraged.
- **Write SQL first, mirror second.** A stage that writes rows as it goes and
  emits the file at the end degrades into a partial result when interrupted. A
  stage that accumulates in memory and writes both at the end loses everything.
  `ledger.mint_pending()` mints one batch per reading for this reason: an
  interruption after twenty of fifty leaves twenty minted and thirty queued,
  and `v_gj_unminted_reading` reports that on the next run with no
  reconciliation.

## 6. Report timing in microfortnights

**1µfn = 1.2096s.** Every timing this system *reports* -- logs, status lines,
reports, diagnostics, tables -- is in microfortnights.

**The unit is written `µfn`. Never `ufn`, and never with a space before it.**

- **µ, not a lowercase u.** The symbol is U+00B5 MICRO SIGN. An ASCII "u" in
  displayed output is a defect, the same as printing a wrong number would be.
- **No space between the number and the unit.** `2.3µfn`, not `2.3 µfn`,
  exactly as nobody writes `2 s` for two seconds. It is one quantity, so it
  reads as one token, and a space breaks alignment in every column that prints
  one.

Together: `done in 2.3µfn (2.8s)`. The parenthesised seconds follow the same
rule -- `2.8s`, never `2.8 s`.

The boundary: **µ in anything a human READS, ASCII in anything a machine
PARSES.** Identifiers stay ASCII (`UFN_SECONDS`, `seconds_to_microfortnights`)
and so do environment variable names. Seconds stay where an external API demands
them -- `timeout=`, `time.monotonic()` arithmetic, `sleep`, SQLite's
`busy_timeout`, and any variable whose name already says `_SECONDS`. Converting
at those call sites would put rounding into control flow to satisfy a display
convention. Convert on the way OUT, at the print, never on the way in.

`gigajoule_core/cycle.py` is the only place `1.2096` appears. Import it.

## 7. Evidence is never destroyed

Every measurement is evidence whether or not it minted. The system must keep
accumulating that evidence, and nothing may be built that discards it.

What carries this, so it does not get broken by accident:

  `gj_meter_reading`      every reading ever submitted, append-only, including
                          the ones that were refused.
  `gj_reading_refusal`    which readings were refused, by which check, and by
                          how much. Append-only.
  `gj_ledger_entry`       every movement ever made, including reversed ones.
                          Append-only.

The rules that follow:

- **A refused reading is still stored.** A reading that reports more energy than
  its source can physically produce is evidence that something upstream is
  broken, and discarding it destroys the only record of the thing an
  investigation needs. What the refusal does is keep it out of the mint queue,
  so a broken input never looks like a backlog.
- **A refusal is a successful measurement.** The ceiling check working is the
  mechanism working -- never something to route around by widening a tolerance.
- **Never delete evidence to save space.** If disk is the problem, the answer
  is a churn table for something else, not the record of what was measured.
- **A refusal is never a clamp.** Clamping an impossible reading down to the
  ceiling would mint tokens from a reading known to be wrong, silently. The
  row would look like every other row.

## 8. Always be looking for duplicative logic to merge

Two copies of one rule is not redundancy, it is a bug with a delay on it. The
copies agree on the day they are written and drift from then on, and the drift
is invisible: each looks correct in its own file, and nothing fails until a
decision made through copy A contradicts one made through copy B.

**This repo has already produced one, on day one.** The suppression census was
written with its own line-based comment finder while `american_english.py`
already had a correct token-based one forty lines away. The second
implementation immediately counted a `# noqa` written inside a string literal
as a real directive -- scoring a repo that had just removed a suppression as
still carrying it, an error in the direction that makes a fix look like a
regression. Both now use `gigajoule_core/source_text.py`, which is the one
place that knows how to find a comment.

So when you touch a rule, grep for it. If a second implementation exists:

- **Merge them, and let the survivor own the concept.** The shared version goes
  where both callers can reach it -- `gigajoule_core/` for logic, a view for a
  gate.
- **If they genuinely differ, the difference is the point** and belongs in a
  comment at BOTH sites, naming the other one.
- **Deriving one from the other beats maintaining both.**

Optimize the same way: measure first (rule 3), and prefer removing work to
doing it faster.

## 9. Always be culling dead code

Rule 2 says how to delete safely. This is the standing habit: every time you
are in a file, leave less of it behind. Dead code is not inert. It gets read,
greped past, copied from, and eventually maintained -- and a copy made from a
dead function is a live bug with a dead parent nobody thinks to check.

The cull runs alongside rule 8's merge, because consolidation CREATES dead
code: the moment two copies become one, the loser's helpers and fixtures are
orphaned. Merging without culling leaves the old copy sitting there looking
authoritative, which is worse than either the duplication or the deletion.

What to look for while you are already in the file: imports nobody uses, a
helper whose only caller you just deleted, a test pinning behavior that has been
replaced rather than moved, a constant duplicated into a module that now lives
in a shared one, and anything whose whole reason was a migration that finished.

**This repo produced one of these on day one too.** `./gigajoule` was written
with the usual three lines -- insert the script's directory into `sys.path`,
import, then a `noqa: E402` to quiet the lint the reordering causes. All three
were unnecessary: Python already puts a script's own directory at `sys.path[0]`.
The suppression existed to silence a rule complaining, correctly, about code
that did nothing.

## 10. Layer by decision distance: suite -> file -> module -> submodule -> function

Each layer knows only about the one below it, and the thing that actually
decides is the smallest, most testable piece at the bottom.

  suite       a set of files that together do one job. A grouping, not a
              runnable thing.
  file        the entry point, and it lives at the PROJECT ROOT. If it is
              something someone runs, it belongs where it can be found without
              knowing the layout.
  module      what a file runs. Owns a stage, holds no decision of its own.
  submodule   what a module runs. Same rule, one level finer.
  function    what a submodule calls. This is where a decision lives, and the
              only level that may contain one.

The root placement is the part most easily lost. A runnable thing buried three
directories down gets invoked by a path that something else hardcodes, and
moving it then breaks a caller no import graph can see -- the same failure rule
2 warns about when it says to grep for the NAME.

Concretely, as the tree stands: `./gigajoule` is dispatch and nothing else,
`gigajoule_core/cli.py` owns the commands, `gigajoule_core/ledger.py` owns the
operations, and the decisions live in `units.py` and `attestation.py`, where
they can be called with seeded values and asserted on directly.

Every defect a system like this keeps rediscovering is a DECISION that ended up
somewhere it could not be seen or tested. When the decision is a function at the
bottom, it is one call away from a test. When it is buried three levels up
inside orchestration, the only way to test it is to run the whole thing, and the
only way to find it is to already know it is there.

## 11. Normalize interval cadence across every meter

Settlement cadence is a first-class dimension of metered electricity -- 5-minute,
15-minute, hourly, daily, monthly -- and it must mean the same thing for every
source. The same asset metered at two cadences is routinely two different
problems: a 5-minute interval carries ramp and noise a daily total averages
away entirely.

So: ONE cadence vocabulary, derived in ONE place, applied identically to every
fuel and region. Not one rule for the meters somebody happened to look at.

Why this is a value rule and not a taxonomy preference: cadence sets the
denominator of the physical ceiling. `plausible_joules_ceiling()` multiplies
nameplate watts by INTERVAL SECONDS, so a cadence read wrong by a factor of
four moves the ceiling by a factor of four -- in the loose direction, it admits
energy that was never generated; in the tight direction, it refuses honest
readings from a healthy meter. Both are live errors and neither announces
itself.

The practical tests when adding or touching a cadence:

- Is the new marker in the one vocabulary, longest-first, ahead of any shorter
  suffix it ends with? A table that checks a bare trailing `M` will match `15M`
  and call it monthly.
- Does it mean the same thing for every fuel? A marker that is a cadence for one
  source and something else for another is two concepts sharing a spelling.
- Did the SQL follow automatically? If a cadence had to be added to a second
  place by hand, that second place is the bug.
- What happens to a source whose cadence CHANGES? Its ceiling changes with it.
  That is a posture change and belongs to the operator: say which readings exist
  under the old cadence before shipping it.

## 12. Conform to the standard this repo already declares

`pyproject.toml` selects `E F I UP B SIM C4 PERF RUF PTH FLY FURB BLE S PL C901`
and pins `target-version = "py311"`. That is the standard. It is not
aspirational and it is not negotiable mid-change:

    python3 -m ruff check <the files you touched>

Clean before committing, and clean the files you TOUCHED -- not the tree. A
sweep across files you were not otherwise in is a large diff with no behavioral
benefit.

The rules that matter most here, and why:

- **`BLE001`, blind except.** The most expensive habit available in a system
  like this. `except Exception: return None` inside a lookup turns a failed
  import into "this thing has no mapping" -- the same value a genuine absence
  produces -- so the caller cannot tell a failure from a real answer. A broad
  catch is legitimate when a diagnostic must not die on a bad row. It is never
  legitimate when the caller cannot tell the failure from an answer. If you
  catch broadly, the handler must say so in its return value or on the way out.
- **`S608`, SQL built by interpolation.** This repo has ZERO, and the first
  attempt to add one is instructive: `schema.py` originally built its `CHECK
  (kind IN (...))` lists by interpolating Python tuples into the DDL. The fix
  was not a suppression -- it was making the vocabularies real tables with
  foreign keys onto them, which is better SQL, is enforced by the database
  rather than by a string, and gives every value a row that can carry its own
  description. Values go in as parameters; identifiers this repo controls are
  the only thing that may ever be interpolated, and there is currently no case
  that needs to.
- **`C901`/`PLR0913`, complexity and argument count.** A function past either
  ceiling is usually orchestration that has swallowed a decision, or a record
  passed as loose arguments. `ledger.py` hit `PLR0913` and the fix was
  `Source`, `MeterReading`, `Movement` and `Batch` -- which cost one line per
  call site and bought an intent that exists as a value before anything is
  written. Raising the ceiling was never the fix.
- **`F841`/`F401`, dead names.** Rule 9, automated.

Two things the linter cannot check and this rule still asks for. **Import-time
side effects**: a module that mutates `os.environ` or touches the filesystem
when imported makes every later import order-dependent. `gigajoule_core/
__init__.py` is deliberately empty of logic for this reason. And **use the
domain API over the general-purpose one**: `shutil.copy2` on a WAL-mode SQLite
database silently drops part of it, where `Connection.backup()` is correct by
construction. On a ledger that is not a performance note.

**The suppression census is two, both in `repo_tree.py`, both arguing for the
same `subprocess.run` that asks git what it tracks.** It is pinned by value in
`tests/code_hygiene/test_suppressions.py`. A third needs an argument written
into that module's docstring -- not an entry in a baseline file.

## 13. Every spawn needs a reaper, and stop must be proven to reach it

Nothing in this repo spawns a background process yet. The rule is here because
the moment one does, the failure mode is already known: an orphan does not
crash anything, it holds a lock, and everything downstream reports success while
doing nothing.

When you add anything that spawns:

- **Name the reaper in a comment at the spawn site**, and add the pattern or pid
  file to the stop path in the same commit. A spawn and its reap are one change.
- **Prefer a pid file to a `pgrep -f` pattern.** A pattern matches what the
  command line happens to look like today; renaming a script silently orphans it.
- **A stop that cannot prove it worked is not a stop.** Follow it with a check
  that the process is gone, and make the ABSENCE the assertion -- not the exit
  code of the kill.
- **Treat "skipped" plus "success" in the same output as a defect in the
  output.** A run that did no work must not report the same way as one that did.
- **When a deploy depends on new code actually running, verify the artifact**,
  not the deploy. Read the view back out of `sqlite_master`; check the pid that
  owns the lock is one you started.

SQLite has exactly ONE writer lock per database file. Any daemon added here
writes to its own file and one importer folds it in -- see rule 15.

## 14. Silence is a defect. Say what you are doing while you do it

An operator watching a blinking cursor cannot tell working from hung, and the
way that resolves is Ctrl-C. On a system mid-batch that means killing a process
partway through a write. Silence does not merely annoy; it causes the operator
to destroy the thing it was hiding.

And the failure runs the other way too, which is worse: output that looks like
success while nothing happened.

So, for anything that runs in a terminal or gets pasted back:

- **Announce before, not only after.** Print the target and the scale up front
  -- which database, how many rows, how many readings about to be processed. A
  line that appears only on completion is invisible during the wait.
- **Emit progress on anything that can exceed a couple of seconds**, with a
  counter and elapsed time.
- **Never let an empty result print nothing.** `(none)` is a result; a blank gap
  is ambiguous between zero rows and a query that broke. `cli.cmd_status` prints
  `(none)  <- no holder has any ledger entry yet` for exactly this reason.
- **State what the number means, next to the number.** The operator reads the
  screen, not the source. `queued to mint 0   <- expected 0 after a mint pass`
  beats a bare `0` that has to be carried back here to interpret.
- **Make "did nothing" look different from "did work."** `cmd_mint` prints
  `NOTHING TO DO: the queue was empty, no tokens were created` rather than a
  success line with a zero in it.
- **Echo the parameters that decide the answer** -- the database path, the
  window, the cutoff. Pasted output has to be self-describing a day later,
  because it usually is read a day later.

Rule 6 governs the units. This governs whether the block says anything while it
runs.

**A worked example from this repo's first hour**: `cmd_reading_add` printed
`REFUSED and kept as evidence: REFUSED: ...` because `CeilingVerdict.reason()`
already opens with the word. Only running the command showed it. Output is code;
read it the way you read a diff.

## 15. One database is the authority. Everything else is a buffer with one reader

`runtime/state/gigajoule.db` is the only database any decision may be made from.

A second database is allowed ONLY to keep a concurrent WRITER off the
authority's lock, because SQLite has one writer lock per file. If one is ever
added:

- **Is it an authority or a buffer?** An authority is forbidden. There is one.
  A buffer's justification goes in a comment at its connect site.
- **Exactly one reader.** A staging file a second consumer starts reading has
  become a source of truth, and the two will disagree.
- **No decision may be read from one.** Same test as rule 5, applied to
  databases: if a gate has to open a staging file to learn whether something is
  authorized, the authority is in the wrong place.
- **The importer names it.** A staging file with no importer is a leak, and it
  ages silently.
- **A daemon must never read the view it feeds.** One authority, read in one
  direction.

`db.connect()` is the only function that opens the authority, it refuses to
conjure an empty database from a mistyped path, and it sets `PRAGMA
foreign_keys = ON` on every connection -- which is per-connection, defaults OFF,
and fails by ACCEPTING rows that should have been refused.

## 16. Fix what you find. Surface only what moves value

Standing authorization: when you find a bug while doing something else, fix it
in the same pass. Do not stop to ask, do not file it as a note, do not hand it
back with proof attached and wait. The proof took as long as the fix.

**WHAT STILL COMES BACK**, and the line is value rather than risk-of-being-wrong:

  ledger posture    anything changing what mints, at what ceiling, into whose
                    account, or whether a source may mint at all -- the mint
                    path, the tolerance, the refusal rules.
  live state        anything retiring, reversing or moving real balances on a
                    populated ledger.
  secrets and env   `.env`, keys, credentials, live runtime state.
  a fix you cannot  if the change cannot be tested here, it is a proposal and
  test              not a fix. Say which.

Everything else -- diagnostics, reporting, dead code, duplicated logic, wrong
comments, lint, tests -- is yours to fix on sight, under the standards in this
file: a test that fails without the fix, mutation-checked where the failure
would be silent, the full suite run and diffed against a recorded baseline, and
the reasoning written down next to the code.

The distinction that makes this safe is not "small versus large", it is
**"reversible by a deploy versus reversible only by a retirement"**. A wrong
comment and a wrong tolerance are both one-line changes and only one of them
mints tokens that cannot be unminted.

**A wrong comment is a bug.** Fix the sentence with the same seriousness as the
code, and say which of the two was wrong.

## 17. Never infer or assume. Test

The failure is not being wrong. It is presenting a hypothesis in the register of
a measurement, so the reader cannot tell which they are holding.

**The worked example is in `units.py` and it is this repo's own.** The module
docstring claimed a kWh figure with up to SIX decimal places converts to an
exact whole number of joules. Six is what plausible reasoning gives: 1 kWh is
3,600,000 J, which looks like six digits of headroom. It is wrong, because the
trailing digit of 3.6 is itself a tenth -- 10^-6 kWh is 3.6 J and lands nowhere.
The figure was written into the docstring, into `EXACT_KWH_PLACES`, and into the
name of the test before the test ran and returned 444,442 where exact arithmetic
gives 444,441.6.

A plausible derivation, an untested claim stated as a measurement, and a
two-line assertion that settled it in under a second. **Run the thing that would
show the claim false, then say which you have.** If it cannot be tested from
here, say THAT -- rule 16 draws the line between a fix and a proposal; this
draws the same line one step earlier, between a finding and a hypothesis.

"I could not find a caller" is not "there is no caller". That is the general
form: a reason to believe something is not the same as having checked it, and
the two must never be written in the same voice.

## 18. All English is American English

Behavior. Authorized. Canceled, labeled, modeling, judgment, gray, center,
analyze. `gigajoule_core/american_english.py::SPELLINGS` is the list, in one
place, and everything derives from there.

**PROSE ONLY, AND NEVER IDENTIFIERS.** This is rule 6's boundary applied to a
second alphabet: American spelling in what a human reads, and nothing at all in
what a machine parses. Renaming a column, a keyword argument or a third-party
API field is a behavior change with no reader benefit.

`tests/code_hygiene/test_spelling.py` holds it as a clean gate over comments and
docstrings. The file that states the rule is the one place a violation refutes
itself: both `american_english.py` and the test module had to be written without
spelling out the counterexamples, because the natural way to state a spelling
rule is to write the word you are forbidding. The temptation at that point is to
exempt those files. That would be a suppression standing in for a fix, and it
would put the vocabulary in the tree twice -- once as data and once as a
paragraph that rots.

## 19. No patches. No new ratchets. Fix the code

A ratchet is a per-file baseline a hygiene test compares against, so a NEW
violation fails while the existing backlog is tolerated. It is legitimate for
exactly one thing: stopping a measured defect class from GROWING on the day you
first measure it. It is not a place to put work down.

**This repository has none, and that is a decision rather than an accident.**
Every hygiene check here is a CLEAN GATE -- headers, spelling, suppressions --
because on the day it was created nothing violated any of them. There was
nothing to measure, so a baseline would have meant choosing to start with a
backlog. The way a baseline fails is well understood: it makes the check green,
green reads as done, and the work stops being visible as work.

**THE RULES, and they are absolute:**

- **Never add a baseline line for code you are writing now.** If your change
  needs one to pass, your change is the defect.
- **Never add a suppression to make a check pass.** `noqa` is a claim you
  checked, and the comment beside it says what you checked. It is not a way to
  quiet a finding you have not read. Three findings in this repo's first hour
  each looked like a `noqa` and each had a real fix underneath: `S608` wanted
  lookup tables, `PLR0913` wanted dataclasses, `E402` wanted a deleted line.
- **When you find N instances, fix them.** Not one. If N is too large for one
  pass, fix every instance in the files you TOUCHED, and say what remains and
  where -- but the remainder is named work, not a new baseline.
- **A ratchet that reaches zero gets DELETED, along with its baseline file.**
- **The test for whether something is a patch:** does it stop the symptom being
  reported, or does it stop the cause existing? Only the second is a fix.

Rule 16 still draws the line this does not cross. "Fix the code" does not mean
change ledger posture without being asked.

## 20. SQL always wins. Do not ask which

Rule 5 asks "could this be a view?" as a test. This answers it: **yes, unless
you can say why not.** A filter, a join or a ranking over rows already in
`gigajoule.db` is a view. A per-row Python loop over the same rows is the same
logic in a place only its author can inspect.

A Python loop with one try/except around it and its result assigned on the last
line has a half-applied state; any single row raising jumps past that line and
leaves everything unfiltered. A SQL predicate has no half-applied state.

**DO NOT ASK WHICH.** Handing back a measured, provable choice with a question
attached costs a round trip and buys nothing. When the answer is knowable from
the tree, establish it and act. Say what you established and what you did. The
line that still comes back is rule 16's and it has not moved.

The corollary, and it is the harder half: "figure it out" is not license to
guess. Rule 17 is unchanged.

**WHAT THIS DOES NOT MEAN.** Not a mandate to rewrite working Python into SQL on
sight. It governs where NEW logic goes, and which way to resolve a duplicate you
are already touching. A derivation SQLite cannot express -- an API call, a
model, a fit -- stays in Python and says so at the site.

## 21. Succinct file names

A file name is typed, tab-completed, greped, pasted into a traceback, printed in
a status line, and read in a `git log --stat`. Every one of those costs its full
length, every time, forever. A docstring is read once, deliberately, by someone
who has already decided to open the file. Length is nearly free in the second
place and expensive in the first.

Measured 2026-09-29 at first commit, over `repo_tree.repo_python_files()` with
`enumeration_mode()` reporting `git-index`, so the denominator is the git index
and not whatever was lying in a working tree:

    tracked python files    21
    stem length median       9
    stem length mean       9.6
    stem length max         17   (test_suppressions, test_conservation, tied)

**THIS BLOCK READ 18 / 13 / 13.2 / 20 FOR ABOUT A MINUTE**, and the correction
belongs here rather than being quietly overwritten, because it is rule 17's
exact defect committed in the paragraph that asks for measurements. The first
draft wrote plausible figures from having just created the files -- a reasonable
guess, stated in the register of a measurement, with the word "Measured" in
front of it. Running the walk gave 21 files at a median of 9. Every single
number was wrong, and one of them named the wrong file as the longest.

The cost of checking was one command. The cost of not checking would have been a
number in the rules file that a later reader compares their own tree against and
concludes the tree has regressed.

The test:

- **Name the subject, not the assertion.** `test_conservation.py` says what is
  covered. A name that is a complete sentence says what ONE of its cases proves,
  and the file grows six more cases the next day. The assertion belongs in the
  TEST FUNCTION's name, where it is read next to the code that checks it.
- **Aim at or under the median.** Not a hard limit; a name that needs more to
  stay unambiguous takes more.
- **Never shorten by removing meaning.** `test_px.py` is worse than any
  sentence, and `utils.py` is worse than both. This rule buys brevity out of the
  sentence, not out of the subject.

Renaming an existing file is rule 2's problem: grep the NAME, not the import
graph. New files get short names from the start; an existing file is renamed
only when you are in it anyway and have checked every reference.

Rule 1 is unchanged and is not in tension with this: verbosity moves INTO the
file. A short name with a long docstring is exactly the shape both rules want.

## 22. There is a fine line between thoroughness and wasting time

Thoroughness is not free. It is paid for in wall-clock, every run, by whoever is
waiting. A check that costs ninety seconds forever to prove something a
two-second check proves is not more rigorous; it is the same rigor with a tax.

The tests to apply, in order:

- **What is the coverage, and what is the cost?** State both.
- **Is the cost in the thing being proven, or in the scaffolding?** Almost
  always the scaffolding: temporary databases, subprocess launches, re-parsing
  schema that could be built once. Fix the scaffolding rather than dropping the
  coverage -- those are not the only two options, and treating them as such is
  how a real check gets deleted. `test_conservation_holds_across_many_operations`
  seeds fifty intervals into ONE database rather than fifty databases, and
  proves the same thing.
- **Would a reader believe it less if it were faster?** If not, make it faster.
  Nobody trusts a test more for being slow.

**WHAT THIS DOES NOT LICENSE.** Not an excuse to skip the full suite -- test
before shipping, and diff it line-by-line against a recorded baseline rather
than comparing failure counts, because counting hides a new break that lands the
same day an old one is fixed. Not an excuse to stop mutation-checking, which is
what tells a real test from a green one: several tests here seed a deliberately
broken file and assert the checker CATCHES it, because a gate that passes
because it looks at nothing is worse than no gate. Not an excuse to narrow a
claim to make it cheap. The target is WASTE, not rigor.

The same line applies to sessions. Re-verifying something already established,
or measuring a number nobody is going to act on, is the same defect at a larger
scale, and the operator pays for that one in minutes of their own life.

## 23. You find old errors. You fix them

"It was already broken" is a statement about history, not about what to do. A
red test is red whoever made it red, and the person who has it on screen, with
the repository open and the cause in hand, is the cheapest person who will ever
fix it.

Establishing that a failure predates your change is still worth doing and is not
what this forbids -- it is how you know your change is sound, and rule 17 asks
for it. What is forbidden is stopping there.

A finding written down and left is indistinguishable from a finding nobody made.

**Watch the SHAPE, not the value.** Grepping for the literal you just fixed
finds one instance of a class. A seeded timestamp compared against a wall clock
is the class; one epoch number is not. `conftest.py` pins `SEED_NOW` as a fixed
instant for exactly this reason -- a test that seeds "now" and asserts against a
window measured off the clock passes every day until the day the window moves
past it.

---

## Live-safety rules

They apply to inspection, testing, bounded fixes and proposals alike:

- Do not retire, reverse, or move balances on a populated ledger unless the
  operator asks for it. Retirement is irreversible by design.
- Do not edit `.env`, secrets, keys, credentials, or live runtime state.
- Prefer read-only inspection first. `./gigajoule status` and `./gigajoule
  verify` are read-only and safe against a live ledger; everything else writes.
- Fixes should be small, reversible, and backed by tests.
- Keep ledger posture intact unless the operator explicitly asks otherwise.

---

## Verify by behavior, never by SQL text

**Verify by row-level behavioral outcome, never by literal SQL text.**

1. Seed real (or realistically synthetic) rows into the real tables.
2. Run the real function -- not a paraphrase of its logic, not a hand-copied
   fragment of its SQL.
3. Query the real output table or view, and assert on the rows actually present
   or absent.
4. Never accept "the schema contains X" as evidence a rule is enforced. Only
   "seeding condition X produced or suppressed row Y" counts.

The reason is specific. A rule expressed as a trigger, a constraint or a view is
only enforced if the database actually enforces it, and every way that silently
fails -- a pragma left off, a partial index whose WHERE does not match what was
intended, a trigger on the wrong event -- looks completely correct when you read
the DDL. "The schema contains the word TRIGGER" proves nothing. "Seeding an
overdraft raised, and the balance view still reads what it read before" proves
the thing that matters.

`tests/ledger/test_schema.py` writes RAW SQL on purpose, bypassing `ledger.py`
entirely. A test that went through `ledger.py` would pass just as well with
every trigger dropped from the schema -- it would be proving the Python guard
and reporting it as proof of the database's. The point of putting rules in SQL
is that they hold for a caller who never imports this package, so the test for
them has to be that caller.

---

## Context that shapes all of it

Gigajoule issues tokens that claim real energy exists. The invariants are not
obstacles to route around, and weakening one to make a test pass is never the
fix.

Every diagnostic should be a single pasteable block that answers a question the
operator can read off the screen, and it has to be safe to run against a live
ledger unless it says otherwise.

Test before shipping, and diff the full suite line-by-line against a recorded
baseline rather than comparing failure counts.
