"""The allocation engine.

Everything here is a pure function of its inputs: given the same list of
Purchases and Consumptions, replay() and generate_statement() always
produce the same result, byte for byte. There is no hidden state, no
month-by-month carry-forward, and no reliance on the order rows were
entered into the system - only on their own dated fields
(purchased_on / worked_on / expires_on).

Design decisions (see README.md for the full write-up):

- Expiry is INCLUSIVE. A block with expires_on == D can still be drawn on
  by work with worked_on == D. worked_on == D + 1 day cannot.
- Allocation order across blocks is strictly (expires_on, purchased_on,
  id) ascending - earliest expiry first, ties broken by earlier purchase,
  further ties broken by id. purchased_on is NOT a gate on eligibility:
  a block purchased after the work was done can still be drawn on, as
  long as it hasn't expired as of the work date.
- Consumption entries (positive or negative) are processed in a single
  canonical timeline ordered by (worked_on, id) ascending - never in
  "entry order" / insertion order. This is what makes a late-arriving
  entry for an already-reported month re-slot itself correctly.
- A negative entry is a correction, not a fresh draw. It reverses hours
  from the SAME task_ref's own allocation history, most-recently-
  allocated block first (LIFO), so a refund goes back to the block it
  actually came from rather than whichever block currently has room.
  Reversal ignores expiry: you can always give hours back to a block
  that has since expired.
- Overdraft is a one-way bucket. Hours consumed once every block has
  expired (or none exist yet) are recorded against OVERDRAFT and never
  auto-repaid by a later purchase; that would require finance to
  explicitly true it up, which is outside this engine's scope.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import (
    OVERDRAFT,
    UNRESOLVED,
    Allocation,
    Consumption,
    ConsumptionAllocation,
    Purchase,
    Statement,
)

ZERO = Decimal("0")


@dataclass
class _BlockState:
    purchase: Purchase
    remaining: Decimal


@dataclass(frozen=True)
class ReplayResult:
    """Full, final ledger for one client: every entry's allocation split."""

    entries: tuple[ConsumptionAllocation, ...]
    blocks_by_id: dict


def replay(purchases: list[Purchase], consumptions: list[Consumption]) -> ReplayResult:
    """Replay a client's complete history and return the final allocation of
    every consumption entry. Assumes all rows belong to a single client;
    generate_statement() does the per-client filtering before calling this.
    """
    blocks_fifo = sorted(purchases, key=lambda p: (p.expires_on, p.purchased_on, p.id))
    block_state = {p.id: _BlockState(purchase=p, remaining=p.hours) for p in purchases}

    canonical_order = sorted(consumptions, key=lambda c: (c.worked_on, c.id))
    task_stack: dict[str, list[dict]] = {}
    entries: list[ConsumptionAllocation] = []

    for c in canonical_order:
        allocations: list[Allocation] = []

        if c.hours > ZERO:
            remaining_to_allocate = c.hours
            for p in blocks_fifo:
                if remaining_to_allocate <= ZERO:
                    break
                if p.expires_on < c.worked_on:
                    continue  # already expired as of the work date
                st = block_state[p.id]
                if st.remaining <= ZERO:
                    continue
                draw = min(st.remaining, remaining_to_allocate)
                st.remaining -= draw
                allocations.append(Allocation(p.id, draw, p.rate_per_hour))
                task_stack.setdefault(c.task_ref, []).append(
                    {"block_id": p.id, "hours": draw, "rate": p.rate_per_hour}
                )
                remaining_to_allocate -= draw
            if remaining_to_allocate > ZERO:
                allocations.append(Allocation(OVERDRAFT, remaining_to_allocate, None))
                task_stack.setdefault(c.task_ref, []).append(
                    {"block_id": OVERDRAFT, "hours": remaining_to_allocate, "rate": None}
                )

        elif c.hours < ZERO:
            refund = -c.hours
            stack = task_stack.setdefault(c.task_ref, [])
            while refund > ZERO and stack:
                receipt = stack[-1]
                give_back = min(receipt["hours"], refund)
                if receipt["block_id"] == OVERDRAFT:
                    pass  # overdraft balance is derived from allocations, not tracked separately
                else:
                    block_state[receipt["block_id"]].remaining += give_back
                allocations.append(Allocation(receipt["block_id"], -give_back, receipt["rate"]))
                receipt["hours"] -= give_back
                refund -= give_back
                if receipt["hours"] == ZERO:
                    stack.pop()
            if refund > ZERO:
                # Correcting more than was ever allocated to this task_ref.
                # No block or overdraft balance is touched; flagged so it is
                # visible in the audit trail rather than silently dropped.
                allocations.append(Allocation(UNRESOLVED, -refund, None))

        entries.append(
            ConsumptionAllocation(
                consumption_id=c.id,
                client=c.client,
                worked_on=c.worked_on,
                task_ref=c.task_ref,
                total_hours=c.hours,
                allocations=tuple(allocations),
            )
        )

    return ReplayResult(entries=tuple(entries), blocks_by_id={p.id: p for p in purchases})


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def generate_statement(
    purchases: list[Purchase],
    consumptions: list[Consumption],
    client: str,
    year: int,
    month: int,
) -> tuple[Statement, list[ConsumptionAllocation]]:
    """Compute the statement and its supporting consumption detail for one
    client and one calendar month. Pure function of the full input lists -
    computing March never depends on having computed February first.
    """
    client_purchases = [p for p in purchases if p.client == client]
    client_consumptions = [c for c in consumptions if c.client == client]

    result = replay(client_purchases, client_consumptions)
    start, end = _month_bounds(year, month)

    # Net hours ever allocated to each block, across the FULL history
    # (a correction dated after a block's expiry can still return hours to
    # it, which is why this always uses the complete replay, never a
    # date-filtered one - see README "retroactive consumption").
    consumed_from_block_total: dict[str, Decimal] = {p.id: ZERO for p in client_purchases}
    # Same total, but only counting entries dated before the month start -
    # this gives the block's remaining balance at the instant the month
    # opened, without needing a second, separate replay.
    consumed_from_block_before_month: dict[str, Decimal] = {p.id: ZERO for p in client_purchases}

    consumed_this_month = ZERO
    consumed_from_blocks_this_month = ZERO
    overdraft_incurred_this_month = ZERO
    overdraft_balance_to_date = ZERO
    detail_this_month: list[ConsumptionAllocation] = []

    for entry in result.entries:
        for alloc in entry.allocations:
            if alloc.block_id == OVERDRAFT:
                if entry.worked_on <= end:
                    overdraft_balance_to_date += alloc.hours
            elif alloc.block_id == UNRESOLVED:
                pass
            else:
                consumed_from_block_total[alloc.block_id] += alloc.hours
                if entry.worked_on < start:
                    consumed_from_block_before_month[alloc.block_id] += alloc.hours

        if start <= entry.worked_on <= end:
            consumed_this_month += entry.total_hours
            detail_this_month.append(entry)
            for alloc in entry.allocations:
                if alloc.block_id == OVERDRAFT:
                    overdraft_incurred_this_month += alloc.hours
                elif alloc.block_id != UNRESOLVED:
                    consumed_from_blocks_this_month += alloc.hours

    opening_balance = ZERO
    for p in client_purchases:
        if p.purchased_on < start and p.expires_on >= start:
            opening_balance += p.hours - consumed_from_block_before_month[p.id]

    purchased_this_month = sum(
        (p.hours for p in client_purchases if start <= p.purchased_on <= end), ZERO
    )

    expired_this_month = ZERO
    for p in client_purchases:
        if start <= p.expires_on <= end:
            expired_this_month += p.hours - consumed_from_block_total[p.id]

    closing_balance = (
        opening_balance
        + purchased_this_month
        - consumed_from_blocks_this_month
        - expired_this_month
    )

    statement = Statement(
        client=client,
        year=year,
        month=month,
        opening_balance=opening_balance,
        purchased_this_month=purchased_this_month,
        consumed_this_month=consumed_this_month,
        expired_this_month=expired_this_month,
        closing_balance=closing_balance,
        overdraft_incurred_this_month=overdraft_incurred_this_month,
        overdraft_balance_to_date=overdraft_balance_to_date,
    )
    return statement, detail_this_month
