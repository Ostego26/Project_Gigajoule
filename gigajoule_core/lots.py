"""Which lots a payment spends, and what a buyer is allowed to insist on.

Role: decision (pure functions, no I/O)
Reads: nothing
Writes: nothing
Can move tokens: no
Live-safe: yes

THIS MODULE IS THE PRICE OF MAKING HOLDINGS PER-LOT, and it is worth paying.

Once a holding is a set of parcels rather than a number, "pay someone 1GJ" stops
being subtraction. Something has to decide WHICH joules leave, and that decision
is economically real: the lots differ in fuel, in region, and in when the energy
was generated, and those are exactly the attributes a buyer is paying for. A
system that picked arbitrarily would deliver a different good on every run and
be unable to say why.

So the selection is one pure function over seeded values, with no database in
sight. It can be asserted on directly, which is the whole reason it lives here
rather than inside the SQL that reads holdings.

THE DEFAULT RULE IS OLDEST VINTAGE FIRST, and the reason is not tradition:

  - Energy attributes age. Every real certificate registry expires them, because
    a claim that last March's solar covers this March's consumption is the claim
    the whole instrument exists to prevent. Spending the oldest first is what
    keeps inventory from aging out unspent.
  - It is deterministic and reproducible. Two auditors replaying the same ledger
    reach the same answer, which "whatever the query returned" does not give you.
  - It is the conservative reading of what the holder owns. Newest-first would
    let a holder keep the freshest attributes while spending down claims that
    are about to become worthless -- profitable, and exactly the behavior a
    registry should not make convenient.

Ties on vintage break on `lot_id`, which is arbitrary but STABLE. An unstable tie
break is a reproducibility bug that only shows up when two lots share a vintage,
which on hourly settlement is most of them.

SELECTION IS EXACT OR IT REFUSES. It returns lots summing to precisely the
requested joules, splitting the final lot to the joule, or it raises naming the
shortfall. It never rounds, never over-delivers "close enough", and never
silently delivers less -- those are the three ways a payment system quietly
loses other people's property.
"""

from __future__ import annotations

from dataclasses import dataclass


class InsufficientHoldings(RuntimeError):
    """No set of eligible lots totals the requested amount.

    Carries the shortfall in the message rather than just failing, because the
    caller's next question is always "by how much, and was it the amount or the
    specification that was the problem" -- and those want different fixes.
    """


@dataclass(frozen=True)
class Holding:
    """One position: this account holds this many joules of this lot."""

    lot_id: str
    joules: int
    vintage_start: int
    vintage_end: int
    fuel: str
    grid_region: str


@dataclass(frozen=True)
class LotSpec:
    """What a buyer insists on. Every field None means "any".

    A spec is a FILTER, never a preference. A partial match is not a match: if a
    buyer asks for solar and only wind is available, the correct answer is a
    refusal, not wind. Softening that anywhere would make the attribute a
    suggestion, and the attribute is the product.
    """

    fuel: str | None = None
    grid_region: str | None = None
    vintage_start: int | None = None
    vintage_end: int | None = None

    def matches(self, holding: Holding) -> bool:
        """Whether one holding is eligible under this spec.

        The vintage test requires the lot to sit ENTIRELY inside the window.
        Overlap would be the looser reading and it is wrong here: a lot spanning
        the boundary carries energy generated outside the window the buyer
        specified, and there is no way to deliver only the part that qualifies
        without splitting the lot's provenance, which a lot by definition does
        not have.
        """
        if self.fuel is not None and holding.fuel != self.fuel:
            return False
        if self.grid_region is not None and holding.grid_region != self.grid_region:
            return False
        if self.vintage_start is not None and holding.vintage_start < self.vintage_start:
            return False
        return not (self.vintage_end is not None and holding.vintage_end > self.vintage_end)

    def describe(self) -> str:
        """One line for a refusal message, so an operator sees what was asked."""
        parts = [
            f"{name}={value}"
            for name, value in (
                ("fuel", self.fuel),
                ("region", self.grid_region),
                ("vintage_start", self.vintage_start),
                ("vintage_end", self.vintage_end),
            )
            if value is not None
        ]
        return ", ".join(parts) if parts else "any lot"


ANY_LOT = LotSpec()


def eligible(holdings: list[Holding], spec: LotSpec = ANY_LOT) -> list[Holding]:
    """The holdings a spec admits, in spend order: oldest vintage, then lot_id."""
    return sorted(
        (h for h in holdings if spec.matches(h)),
        key=lambda h: (h.vintage_start, h.lot_id),
    )


def select_lots(
    holdings: list[Holding], joules: int, spec: LotSpec = ANY_LOT
) -> list[tuple[str, int]]:
    """Pick lots totalling exactly `joules`, oldest vintage first.

    Returns [(lot_id, joules), ...] summing to precisely `joules`. Raises
    `InsufficientHoldings` if the eligible lots cannot cover it.
    """
    if joules <= 0:
        raise ValueError(f"select_lots needs a positive amount, got {joules}")

    candidates = eligible(holdings, spec)
    available = sum(h.joules for h in candidates)
    if available < joules:
        total_held = sum(h.joules for h in holdings)
        raise InsufficientHoldings(
            f"need {joules}J matching [{spec.describe()}] but only {available}J is "
            f"eligible (holding {total_held}J across {len(holdings)} lot(s) in total). "
            f"Short by {joules - available}J."
            + (
                ""
                if available == total_held
                else " The shortfall is in the SPECIFICATION, not the amount: there "
                "are enough joules held, they just do not match what was asked for."
            )
        )

    picked: list[tuple[str, int]] = []
    remaining = joules
    for holding in candidates:
        if remaining == 0:
            break
        take = min(holding.joules, remaining)
        picked.append((holding.lot_id, take))
        remaining -= take
    return picked
