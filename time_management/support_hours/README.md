# Prepaid support-hours statement engine

A standalone engine for computing a defensible monthly statement of prepaid
support hours: FIFO-by-expiry allocation across purchased blocks, a full
block-by-block split for every consumption entry, an overdraft bucket, and
byte-identical, replayable output.

No Frappe, no bench, no database. Plain Python (stdlib only) plus
`unittest`. It lives inside the `time_management` app's source tree
(`time_management/support_hours/`) but does not import `frappe` anywhere.

## How to run it

```bash
cd apps/time_management

# Run the full test suite (21 tests, one or more per edge case below)
python3 -m unittest time_management.support_hours.tests.test_engine -v

# Generate a statement + consumption detail from the sample data
python3 -m time_management.support_hours.cli \
    --input time_management/support_hours/sample_data/sample_input.json \
    --client ACME --year 2026 --month 3
```

No `pip install` is required - everything used (`dataclasses`, `decimal`,
`datetime`, `json`, `argparse`, `unittest`) is in the Python 3.10+ standard
library.

## Files

- `models.py` - plain dataclasses: `Purchase`, `Consumption`, `Allocation`,
  `Statement`. `to_decimal()` is the single place numeric input is parsed.
- `engine.py` - `replay()` (the FIFO/overdraft/correction ledger) and
  `generate_statement()` (slices the replay into one client-month).
- `io_json.py` - loading a dataset from JSON, deterministic JSON output.
- `cli.py` - a thin command-line wrapper around the two functions above.
- `sample_data/sample_input.json` - the dataset used below.
- `sample_data/sample_input_retroactive.json` - the same dataset plus one
  late-arriving January entry (`CN9`), used to demonstrate the retroactive
  edge case.
- `sample_data/sample_input_manual_test.json` - a smaller, easy-to-check-
  by-hand dataset (client `TESTCO`, two blocks, no ties, no corrections)
  for manually walking through the CLI - see "Manual testing" below.
- `tests/test_engine.py` - the test suite.

## Edge-case decisions

- **Retroactive consumption.** Every consumption entry (and correction) is
  bucketed into a statement purely by its own `worked_on` date, never by
  when it was entered into the system, and the whole ledger is recomputed
  from the complete input every time (no cached per-month state). Adding a
  late entry dated back into an already-reported month and re-running that
  month's statement therefore shows different numbers immediately - see
  `TestRetroactiveConsumption` and `sample_input_retroactive.json` (January's
  closing balance moves from 8 to 7 hours, and February's opening balance
  moves with it).
- **Expiry boundary.** `expires_on` is **inclusive**: a block expiring
  `2026-03-31` can still be drawn on by work dated `2026-03-31`, but not by
  work dated `2026-04-01`. This one rule (`block.expires_on >= worked_on`)
  is used everywhere a block's eligibility is checked - allocation,
  opening/closing balance snapshots, and the "expired this month" figure.
- **Partial consumption across 3+ blocks.** A single consumption entry
  walks the FIFO-by-expiry block list and keeps drawing from the next
  block until its hours are satisfied, so a large enough entry naturally
  spans any number of blocks. Each block keeps its own rate, so the split
  is rate-accurate, not just hours-accurate. See
  `test_partial_consumption_across_three_blocks`.
- **Zero and negative hours.** Zero-hour entries are a recorded no-op (no
  allocation, no balance change, still present in the audit trail). A
  negative entry is a **correction**, not a fresh draw: it reverses hours
  from the *same `task_ref`'s* own allocation history, most-recently-drawn
  block first (LIFO), so a `-2` correction gives hours back to the block it
  actually came from, not to whichever block currently has room. A
  correction that ignores expiry - you can always give hours back to a
  block that has since expired, since you're undoing a past charge, not
  making a new draw. A correction that asks for more than was ever
  allocated to its `task_ref` is capped at what exists and the remainder is
  recorded against a separate `UNRESOLVED` bucket rather than silently
  dropped or misapplied to an unrelated block.
- **Block bought after work was done.** Block eligibility and FIFO order
  depend only on `expires_on` (and, for ties, `purchased_on`) - never on
  whether `purchased_on` is before or after the consumption's `worked_on`.
  A block purchased on the 20th can still be the block a consumption dated
  the 15th draws from, as long as it hasn't expired. See
  `test_earliest_expiry_consumed_first_even_if_purchased_later` and `P4` /
  `CN5` in the sample dataset.
- **Floating-point hours.** All hours and rates are `decimal.Decimal`,
  parsed from `str` or `int` input only - `to_decimal()` raises `TypeError`
  on a raw Python `float`, because by the time a value like `0.1` reaches
  Python as a float it has already lost precision; the caller must supply
  `"0.1"`, not `0.1`. `0.1 + 0.2` therefore reports as exactly `0.3` (see
  `TestFloatingPointHours`), and every JSON output value is written as a
  fixed-point string (`format(d, "f")`), never through a float, so nothing
  reintroduces binary rounding on the way out.
- **Determinism / replayability.** `replay()` and `generate_statement()`
  are pure functions of their input lists: the FIFO block order and the
  consumption processing order are both computed from the data itself
  (`(expires_on, purchased_on, id)` and `(worked_on, id)` respectively),
  never from input list order or insertion order. A statement for any
  month is a filter over one full recompute of the client's entire
  history, so March never depends on February having been computed first.
  `io_json.to_json()` serializes with sorted keys and fixed separators, so
  two runs produce byte-identical output (`TestDeterminism`).
- **Overdraft.** Once every block that could cover an entry has expired (or
  none exist yet), the shortfall is recorded against a distinct
  `OVERDRAFT` bucket - never dropped, never forced into an expired block.
  Overdraft is a one-way bucket in this engine: a later purchase creates a
  new block with its own balance and does not automatically pay down a
  prior overdraft. That reconciliation, if wanted, is a deliberate business
  decision outside this engine's scope, so the statement reports overdraft
  incurred this month and cumulative overdraft to date as two separate,
  explicit figures rather than silently netting them against future
  purchases.
- **IDs.** Purchase and consumption `id`s are assumed to be stable and
  consistently orderable (e.g. all ints, or all strings of the same
  padding) within one client's data, since they are the deterministic
  tie-breaker in both sort orders above.

## Statement fields

For a given client and month:

| field | meaning |
|---|---|
| `opening_balance` | remaining hours, across still-open blocks, as of the first instant of the month |
| `purchased_this_month` | hours purchased with `purchased_on` in the month |
| `consumed_this_month` | net hours consumed (positive entries + corrections) with `worked_on` in the month, block-sourced and overdraft-sourced combined |
| `expired_this_month` | hours left unused in blocks whose `expires_on` falls in the month, using the complete final ledger (so a later correction can still change this figure for a past month) |
| `closing_balance` | `opening_balance + purchased_this_month - (hours drawn from blocks this month) - expired_this_month` |
| `overdraft_incurred_this_month` | the portion of this month's consumption that had no block to draw from |
| `overdraft_balance_to_date` | cumulative overdraft across all entries up to the end of this month |

The consumption detail (the list returned alongside the statement) gives,
for every entry with `worked_on` in the month, its full block-by-block
split (`block_id`, `hours`, `rate_per_hour`), including negative lines for
corrections and the `OVERDRAFT` / `UNRESOLVED` sentinels where relevant -
this is what lets finance answer "which block did this hour come from" for
any line on the statement.

## Reproducing the walkthrough

The sample dataset (`sample_data/sample_input.json`) covers, for one client
(`ACME`) across January-April 2026: full FIFO utilization, an
expiry-boundary draw (`P1` expires 2026-01-31, consumed same day by
`CN2`), a two-block split (`CN2` spanning `P1` and `P2`), a block left
partly unused past its expiry (`P2`, 7 of 10 hours go unused after
February), a block purchased after some of the work it later covers
(`P4`, purchased 2026-03-20, drawn on by `CN5` dated 2026-03-15), a
negative correction on that same block (`CN6`), and an overdraft case in
April (`CN8`, after every block has expired). Run the CLI command above for
months 1 through 4 to see the numbers; run it again against
`sample_input_retroactive.json` for months 1 and 2 to see January's and
February's statements change because of the late entry `CN9`.

## Manual testing

For hand-checking without the ACME dataset's tie-breaks and corrections in
the way, use `sample_input_manual_test.json` (client `TESTCO`, two blocks:
`B1` 8h expiring 2026-05-31, `B2` 6h expiring 2026-06-30):

```bash
for m in 5 6 7; do
  python3 -m time_management.support_hours.cli \
    --input time_management/support_hours/sample_data/sample_input_manual_test.json \
    --client TESTCO --year 2026 --month $m
done
```

Expected, by hand: May consumes 9h (5 from `B1`, then 3 more from `B1` +
1 spilling into `B2` - a 2-block split), closing at 5h. June's `B2` expires
with 3h unused after one more 2h draw. July has no blocks left, so its 5h
of work lands entirely in `OVERDRAFT`. `test_manual_test_sample_reconciles_month_by_month`
asserts these same numbers.
