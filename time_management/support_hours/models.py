"""Plain data model for the prepaid support-hours engine.

No Frappe imports here on purpose: this module is usable standalone,
with no bench and no database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Union

Numeric = Union[str, int, Decimal]

# Sentinel "block ids" used inside an allocation to mean something other
# than a real purchased block. Purchase ids must not collide with these.
OVERDRAFT = "OVERDRAFT"
UNRESOLVED = "UNRESOLVED"


def to_decimal(value: Numeric) -> Decimal:
    """Convert input to Decimal without ever passing through a binary float.

    Accepts str, int or Decimal. A raw float is deliberately NOT accepted:
    float(0.1) already lost precision before it reached this function, so
    the caller must supply hours/rates as strings (or ints) in the source
    data. This is the one guard that keeps 0.1 + 0.2 == 0.3 exactly.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise TypeError("bool is not a valid numeric input")
    if isinstance(value, (str, int)):
        return Decimal(value)
    raise TypeError(
        f"Refusing to convert {type(value).__name__} to Decimal directly; "
        "pass hours/rates as str or int (e.g. '0.1', not 0.1)."
    )


def to_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Purchase:
    id: str
    client: str
    hours: Decimal
    purchased_on: date
    expires_on: date
    rate_per_hour: Decimal

    @staticmethod
    def parse(row: dict) -> "Purchase":
        return Purchase(
            id=str(row["id"]),
            client=str(row["client"]),
            hours=to_decimal(row["hours"]),
            purchased_on=to_date(row["purchased_on"]),
            expires_on=to_date(row["expires_on"]),
            rate_per_hour=to_decimal(row["rate_per_hour"]),
        )


@dataclass(frozen=True)
class Consumption:
    id: str
    client: str
    hours: Decimal
    worked_on: date
    task_ref: str

    @staticmethod
    def parse(row: dict) -> "Consumption":
        return Consumption(
            id=str(row["id"]),
            client=str(row["client"]),
            hours=to_decimal(row["hours"]),
            worked_on=to_date(row["worked_on"]),
            task_ref=str(row["task_ref"]),
        )


@dataclass(frozen=True)
class Allocation:
    """One line of a split: `hours` drawn from `block_id`.

    block_id is either a real Purchase.id, OVERDRAFT (no block was
    available), or UNRESOLVED (a correction tried to return more hours
    than were ever allocated to its task_ref - see README).
    A negative `hours` value means hours were given back to the block
    (a correction), not drawn from it.
    """

    block_id: str
    hours: Decimal
    rate_per_hour: Decimal | None


@dataclass(frozen=True)
class ConsumptionAllocation:
    consumption_id: str
    client: str
    worked_on: date
    task_ref: str
    total_hours: Decimal
    allocations: tuple[Allocation, ...]


@dataclass(frozen=True)
class Statement:
    client: str
    year: int
    month: int
    opening_balance: Decimal
    purchased_this_month: Decimal
    consumed_this_month: Decimal
    expired_this_month: Decimal
    closing_balance: Decimal
    overdraft_incurred_this_month: Decimal
    overdraft_balance_to_date: Decimal
