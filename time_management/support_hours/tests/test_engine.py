import os
import unittest
from decimal import Decimal

from ..engine import generate_statement, replay
from ..models import OVERDRAFT, UNRESOLVED, Consumption, Purchase
from ..io_json import detail_to_dict, load_dataset, statement_to_dict, to_json


def P(id, hours, purchased_on, expires_on, rate="100", client="C1"):
    return Purchase.parse(
        {
            "id": id,
            "client": client,
            "hours": hours,
            "purchased_on": purchased_on,
            "expires_on": expires_on,
            "rate_per_hour": rate,
        }
    )


def C(id, hours, worked_on, task_ref, client="C1"):
    return Consumption.parse(
        {"id": id, "client": client, "hours": hours, "worked_on": worked_on, "task_ref": task_ref}
    )


def allocations_by_block(entry):
    return {a.block_id: a.hours for a in entry.allocations}


class TestFifoAllocation(unittest.TestCase):
    def test_earliest_expiry_consumed_first_even_if_purchased_later(self):
        # P purchased AFTER work was done, but is the only eligible block.
        purchases = [
            P("P1", "5", "2026-01-01", "2026-01-10"),
            P("P2", "5", "2026-01-20", "2026-02-28"),  # purchased later than the work below
        ]
        consumptions = [C("CN1", "3", "2026-01-05", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(allocations_by_block(result.entries[0]), {"P1": Decimal("3")})

    def test_tie_break_by_purchased_on_when_expiry_matches(self):
        purchases = [
            P("LATE", "5", "2026-01-15", "2026-02-28"),
            P("EARLY", "5", "2026-01-01", "2026-02-28"),  # same expiry, purchased earlier
        ]
        consumptions = [C("CN1", "3", "2026-01-20", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(allocations_by_block(result.entries[0]), {"EARLY": Decimal("3")})

    def test_purchased_later_can_still_be_consumed_before_a_currently_open_block(self):
        # Between two open blocks, the one with the EARLIER expiry wins,
        # regardless of which was purchased first.
        purchases = [
            P("SOON_EXPIRY_LATE_PURCHASE", "5", "2026-02-01", "2026-01-31"),
            P("LATER_EXPIRY_EARLY_PURCHASE", "5", "2026-01-01", "2026-03-31"),
        ]
        consumptions = [C("CN1", "2", "2026-01-15", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(
            allocations_by_block(result.entries[0]),
            {"SOON_EXPIRY_LATE_PURCHASE": Decimal("2")},
        )


class TestExpiryBoundary(unittest.TestCase):
    """expires_on is inclusive: work dated exactly on the expiry date can
    still draw from the block; the next day it cannot."""

    def test_work_on_expiry_date_is_allowed(self):
        purchases = [P("P1", "5", "2026-01-01", "2026-03-31")]
        consumptions = [C("CN1", "2", "2026-03-31", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(allocations_by_block(result.entries[0]), {"P1": Decimal("2")})

    def test_work_the_day_after_expiry_is_not_allowed(self):
        purchases = [P("P1", "5", "2026-01-01", "2026-03-31")]
        consumptions = [C("CN1", "2", "2026-04-01", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(allocations_by_block(result.entries[0]), {OVERDRAFT: Decimal("2")})


class TestSplitAcrossBlocks(unittest.TestCase):
    def test_partial_consumption_across_three_blocks(self):
        purchases = [
            P("P1", "2", "2026-01-01", "2026-01-10", rate="100"),
            P("P2", "3", "2026-01-01", "2026-01-20", rate="110"),
            P("P3", "10", "2026-01-01", "2026-01-31", rate="120"),
        ]
        consumptions = [C("CN1", "7", "2026-01-05", "T-1")]
        result = replay(purchases, consumptions)
        got = allocations_by_block(result.entries[0])
        self.assertEqual(got, {"P1": Decimal("2"), "P2": Decimal("3"), "P3": Decimal("2")})
        rates = {a.block_id: a.rate_per_hour for a in result.entries[0].allocations}
        self.assertEqual(rates, {"P1": Decimal("100"), "P2": Decimal("110"), "P3": Decimal("120")})


class TestZeroAndNegativeHours(unittest.TestCase):
    def test_zero_hour_entry_is_a_recorded_no_op(self):
        purchases = [P("P1", "5", "2026-01-01", "2026-01-31")]
        consumptions = [C("CN1", "0", "2026-01-05", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(result.entries[0].allocations, ())

    def test_negative_correction_returns_hours_to_the_block_it_actually_drew_from(self):
        # Same task_ref draws 2h from P1 (earliest expiry) then 3h more,
        # spilling into P2. The block P1 empties out, P2 remains "convenient"
        # (has free room). A -2h correction on the SAME task must come back
        # out of P2 (the block it was actually drawn from last), not P1.
        purchases = [
            P("P1", "2", "2026-01-01", "2026-01-10"),
            P("P2", "10", "2026-01-01", "2026-02-28"),
        ]
        consumptions = [
            C("CN1", "5", "2026-01-05", "T-1"),  # 2 from P1, 3 from P2
            C("CN2", "-2", "2026-01-06", "T-1"),  # correction
        ]
        result = replay(purchases, consumptions)
        first, second = result.entries
        self.assertEqual(allocations_by_block(first), {"P1": Decimal("2"), "P2": Decimal("3")})
        self.assertEqual(allocations_by_block(second), {"P2": Decimal("-2")})
        # P1 must be untouched by the refund - it was fully consumed and stays that way.
        total_from_p1 = sum(
            (a.hours for e in result.entries for a in e.allocations if a.block_id == "P1"),
            Decimal("0"),
        )
        self.assertEqual(Decimal("2") - total_from_p1, Decimal("0"))

    def test_correction_can_give_hours_back_to_an_already_expired_block(self):
        purchases = [P("P1", "5", "2026-01-01", "2026-01-10")]
        consumptions = [
            C("CN1", "3", "2026-01-05", "T-1"),
            C("CN2", "-1", "2026-02-01", "T-1"),  # dated after P1 expired
        ]
        result = replay(purchases, consumptions)
        self.assertEqual(allocations_by_block(result.entries[1]), {"P1": Decimal("-1")})

    def test_over_correction_beyond_ever_allocated_is_flagged_unresolved(self):
        purchases = [P("P1", "5", "2026-01-01", "2026-01-31")]
        consumptions = [
            C("CN1", "2", "2026-01-05", "T-1"),
            C("CN2", "-5", "2026-01-06", "T-1"),  # more than was ever drawn
        ]
        result = replay(purchases, consumptions)
        got = allocations_by_block(result.entries[1])
        self.assertEqual(got, {"P1": Decimal("-2"), UNRESOLVED: Decimal("-3")})


class TestOverdraft(unittest.TestCase):
    def test_hours_after_all_blocks_expired_go_to_overdraft_not_dropped(self):
        purchases = [P("P1", "3", "2026-01-01", "2026-01-10")]
        consumptions = [C("CN1", "5", "2026-01-05", "T-1")]
        result = replay(purchases, consumptions)
        self.assertEqual(
            allocations_by_block(result.entries[0]), {"P1": Decimal("3"), OVERDRAFT: Decimal("2")}
        )

    def test_overdraft_when_no_blocks_exist_at_all(self):
        consumptions = [C("CN1", "4", "2026-01-05", "T-1")]
        result = replay([], consumptions)
        self.assertEqual(allocations_by_block(result.entries[0]), {OVERDRAFT: Decimal("4")})


class TestFloatingPointHours(unittest.TestCase):
    def test_point_one_plus_point_two_is_exact(self):
        purchases = [P("P1", "10", "2026-01-01", "2026-01-31")]
        consumptions = [
            C("CN1", "0.1", "2026-01-05", "T-1"),
            C("CN2", "0.2", "2026-01-06", "T-2"),
        ]
        statement, _ = generate_statement(purchases, consumptions, "C1", 2026, 1)
        self.assertEqual(statement.consumed_this_month, Decimal("0.3"))
        self.assertNotEqual(str(statement.consumed_this_month), "0.30000000000000004")

    def test_rejects_raw_float_input_to_avoid_silent_precision_loss(self):
        with self.assertRaises(TypeError):
            Purchase.parse(
                {
                    "id": "P1",
                    "client": "C1",
                    "hours": 0.1,
                    "purchased_on": "2026-01-01",
                    "expires_on": "2026-01-31",
                    "rate_per_hour": "100",
                }
            )


class TestRetroactiveConsumption(unittest.TestCase):
    def test_late_entry_in_an_already_reported_month_changes_that_statement(self):
        # Block expires in February, not January, so the extra hour of work
        # reduces January's leftover balance rather than just moving hours
        # from one already-zero bucket to another.
        purchases = [P("P1", "10", "2026-01-01", "2026-02-28")]
        consumptions_before = [C("CN1", "4", "2026-01-10", "T-1")]
        statement_before, _ = generate_statement(purchases, consumptions_before, "C1", 2026, 1)

        # A forgotten entry for January work is entered later, dated back into January.
        consumptions_after = consumptions_before + [C("CN2", "3", "2026-01-15", "T-2")]
        statement_after, _ = generate_statement(purchases, consumptions_after, "C1", 2026, 1)

        self.assertEqual(statement_before.consumed_this_month, Decimal("4"))
        self.assertEqual(statement_after.consumed_this_month, Decimal("7"))
        self.assertNotEqual(statement_before.closing_balance, statement_after.closing_balance)
        self.assertEqual(statement_after.closing_balance, Decimal("3"))

    def test_retroactive_entry_does_not_require_recomputing_later_months_first(self):
        purchases = [P("P1", "10", "2026-01-01", "2026-01-31")]
        consumptions = [
            C("CN1", "4", "2026-01-10", "T-1"),
            C("CN2", "3", "2026-01-15", "T-2"),
        ]
        # Compute March (empty) with no dependency on January/February having run.
        statement_march, _ = generate_statement(purchases, consumptions, "C1", 2026, 3)
        self.assertEqual(statement_march.opening_balance, Decimal("0"))
        self.assertEqual(statement_march.consumed_this_month, Decimal("0"))


class TestStatementArithmetic(unittest.TestCase):
    def test_opening_purchased_consumed_expired_closing_reconcile(self):
        purchases = [
            P("P1", "10", "2026-01-01", "2026-01-31", rate="100"),
            P("P2", "5", "2026-01-01", "2026-02-28", rate="90"),
        ]
        consumptions = [
            C("CN1", "6", "2026-01-10", "T-1"),  # all from P1
        ]
        statement, detail = generate_statement(purchases, consumptions, "C1", 2026, 1)
        self.assertEqual(statement.opening_balance, Decimal("0"))
        self.assertEqual(statement.purchased_this_month, Decimal("15"))
        self.assertEqual(statement.consumed_this_month, Decimal("6"))
        self.assertEqual(statement.expired_this_month, Decimal("4"))  # 10 - 6 unused on P1
        self.assertEqual(statement.closing_balance, Decimal("5"))  # P2 untouched, still open
        self.assertEqual(statement.overdraft_incurred_this_month, Decimal("0"))
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0].consumption_id, "CN1")


SAMPLE_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")


class TestDeterminism(unittest.TestCase):
    def test_running_twice_gives_byte_identical_output(self):
        purchases, consumptions = load_dataset(os.path.join(SAMPLE_DATA_DIR, "sample_input.json"))
        s1, d1 = generate_statement(purchases, consumptions, "ACME", 2026, 3)
        s2, d2 = generate_statement(purchases, consumptions, "ACME", 2026, 3)
        self.assertEqual(to_json(statement_to_dict(s1)), to_json(statement_to_dict(s2)))
        self.assertEqual(
            to_json([detail_to_dict(e) for e in d1]),
            to_json([detail_to_dict(e) for e in d2]),
        )

    def test_output_order_of_input_rows_does_not_affect_result(self):
        purchases = [
            P("P1", "5", "2026-01-01", "2026-01-31"),
            P("P2", "5", "2026-01-01", "2026-02-28"),
        ]
        consumptions = [
            C("CN1", "3", "2026-01-05", "T-1"),
            C("CN2", "4", "2026-01-06", "T-2"),
        ]
        s1, _ = generate_statement(purchases, consumptions, "C1", 2026, 1)
        s2, _ = generate_statement(list(reversed(purchases)), list(reversed(consumptions)), "C1", 2026, 1)
        self.assertEqual(s1, s2)


class TestSampleDataset(unittest.TestCase):
    """Sanity-checks the shipped sample_data files load and produce sane totals."""

    def _load(self, name):
        return load_dataset(os.path.join(SAMPLE_DATA_DIR, name))

    def test_sample_input_loads_and_reconciles_month_by_month(self):
        purchases, consumptions = self._load("sample_input.json")
        prev_closing = None
        for year, month in [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]:
            statement, _ = generate_statement(purchases, consumptions, "ACME", year, month)
            if prev_closing is not None:
                self.assertEqual(statement.opening_balance, prev_closing)
            prev_closing = statement.closing_balance
        self.assertEqual(prev_closing, Decimal("0"))

    def test_retroactive_sample_changes_january_and_flows_into_february(self):
        purchases, consumptions = self._load("sample_input.json")
        r_purchases, r_consumptions = self._load("sample_input_retroactive.json")

        jan_before, _ = generate_statement(purchases, consumptions, "ACME", 2026, 1)
        jan_after, _ = generate_statement(r_purchases, r_consumptions, "ACME", 2026, 1)
        self.assertNotEqual(jan_before.closing_balance, jan_after.closing_balance)

        feb_after, _ = generate_statement(r_purchases, r_consumptions, "ACME", 2026, 2)
        self.assertEqual(feb_after.opening_balance, jan_after.closing_balance)

    def test_manual_test_sample_reconciles_month_by_month(self):
        # A simpler dataset (two blocks, no ties, no corrections) meant for
        # a human to check by hand while running the CLI - see README.
        purchases, consumptions = self._load("sample_input_manual_test.json")
        expected_closing = {5: Decimal("5"), 6: Decimal("0"), 7: Decimal("0")}
        prev_closing = None
        for month in (5, 6, 7):
            statement, _ = generate_statement(purchases, consumptions, "TESTCO", 2026, month)
            if prev_closing is not None:
                self.assertEqual(statement.opening_balance, prev_closing)
            self.assertEqual(statement.closing_balance, expected_closing[month])
            prev_closing = statement.closing_balance
        self.assertEqual(prev_closing, Decimal("0"))
        july_statement, _ = generate_statement(purchases, consumptions, "TESTCO", 2026, 7)
        self.assertEqual(july_statement.overdraft_incurred_this_month, Decimal("5"))


if __name__ == "__main__":
    unittest.main()
