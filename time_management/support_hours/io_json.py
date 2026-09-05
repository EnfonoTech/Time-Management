"""JSON in/out helpers.

Kept separate from engine.py so the engine itself has zero I/O concerns -
it only ever touches the dataclasses in models.py.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from .models import Consumption, ConsumptionAllocation, Purchase, Statement


def load_dataset(path: str) -> tuple[list[Purchase], list[Consumption]]:
    with open(path) as f:
        data = json.load(f)
    purchases = [Purchase.parse(row) for row in data.get("purchases", [])]
    consumptions = [Consumption.parse(row) for row in data.get("consumption", [])]
    return purchases, consumptions


def _default(obj):
    if isinstance(obj, Decimal):
        # Fixed-point string, never scientific notation, never a float.
        return format(obj, "f")
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def statement_to_dict(statement: Statement) -> dict:
    return asdict(statement)


def detail_to_dict(entry: ConsumptionAllocation) -> dict:
    d = asdict(entry)
    return d


def to_json(obj) -> str:
    """Deterministic serialization: sorted keys, fixed separators, Decimals
    as exact fixed-point strings. Running this twice on the same input
    yields byte-identical output.
    """
    return json.dumps(obj, default=_default, sort_keys=True, separators=(",", ":"))


def to_pretty_json(obj) -> str:
    return json.dumps(obj, default=_default, sort_keys=True, indent=2)
