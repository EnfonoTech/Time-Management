"""Command-line demo: generate a statement + consumption detail from a JSON
dataset. No Frappe, no bench, no database - plain Python.

Usage:
    python3 -m time_management.support_hours.cli \\
        --input sample_data/sample_input.json \\
        --client ACME --year 2026 --month 3
"""

from __future__ import annotations

import argparse
import sys

from .engine import generate_statement
from .io_json import detail_to_dict, load_dataset, statement_to_dict, to_pretty_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate a prepaid support-hours statement")
    parser.add_argument("--input", required=True, help="path to the input JSON dataset")
    parser.add_argument("--client", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--month", required=True, type=int)
    args = parser.parse_args(argv)

    purchases, consumptions = load_dataset(args.input)
    statement, detail = generate_statement(purchases, consumptions, args.client, args.year, args.month)

    print("=== Statement ===")
    print(to_pretty_json(statement_to_dict(statement)))
    print()
    print("=== Consumption detail ===")
    print(to_pretty_json([detail_to_dict(e) for e in detail]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
