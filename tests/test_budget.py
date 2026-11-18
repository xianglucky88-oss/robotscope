import unittest

from tests.support import make_state
from robotscope.budget import Budget


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.budget = Budget(self.state, limits=3, reservation_ttl=100)

    def test_reserve_and_consume(self):
        r = self.budget.reserve("q1", 1, host="a", now=10.0)
        self.assertTrue(r.granted)
        self.assertEqual(r.state, "reserved")
        self.assertEqual(self.budget.summary(now=10.0)["used"], 1.0)
        self.assertEqual(self.budget.summary(now=10.0)["remaining"], 2.0)

    def test_duplicate_reservation_does_not_double_charge(self):
        self.budget.reserve("q1", 1, host="a", now=10.0)
        again = self.budget.reserve("q1", 1, host="a", now=11.0)
        self.assertTrue(again.granted)
        self.assertTrue(again.duplicate)
        self.assertEqual(self.budget.summary(now=11.0)["used"], 1.0)

    def test_exhaustion_is_explicit(self):
        for i in range(3):
            self.assertTrue(self.budget.reserve(f"q{i}", 1, now=10.0).granted)
        denied = self.budget.reserve("q4", 1, now=10.0)
        self.assertFalse(denied.granted)
        self.assertEqual(denied.reason, "budget_exhausted")
        self.assertEqual(denied.retry_at, 86400.0)
        self.assertEqual(denied.shortfall, 1.0)

    def test_reserved_is_refundable(self):
        self.budget.reserve("q1", 1, now=10.0)
        result = self.budget.release("q1", now=11.0)
        self.assertTrue(result.released)
        self.assertEqual(result.refunded, 1.0)
        self.assertEqual(self.budget.summary(now=11.0)["used"], 0.0)
        # duplicate release is harmless
        self.assertFalse(self.budget.release("q1", now=12.0).released)

    def test_in_flight_is_not_refundable(self):
        self.budget.reserve("q1", 1, now=10.0)
        self.budget.mark_in_flight("q1", now=11.0)
        result = self.budget.release("q1", now=12.0)
        self.assertFalse(result.released)
        self.assertEqual(result.reason, "not_refundable_after_dispatch")
        self.assertEqual(self.budget.summary(now=12.0)["used"], 1.0)

    def test_unknown_outcome_stays_charged(self):
        self.budget.reserve("q1", 1, now=10.0)
        self.budget.mark_in_flight("q1", now=11.0)
        self.assertTrue(self.budget.mark_unknown("q1", now=12.0))
        self.assertFalse(self.budget.release("q1", now=13.0).released)
        summary = self.budget.summary(now=13.0)
        self.assertEqual(summary["unknown"], 1.0)
        self.assertEqual(summary["remaining"], 2.0)

    def test_receipt_after_release_recharges(self):
        self.budget.reserve("q1", 1, now=10.0)
        self.budget.release("q1", reason="assumed_unexecuted", now=11.0)
        receipt = self.budget.commit("q1", executed_at=10.5, now=12.0)
        self.assertTrue(receipt.accepted)
        self.assertTrue(receipt.late is False)
        self.assertEqual(receipt.reason, "receipt_after_release")
        self.assertEqual(self.budget.summary(now=12.0)["used"], 1.0)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.budget = Budget(self.state, limits=5)

    def test_late_receipt_keeps_execution_day(self):
        self.budget.reserve("q1", 1, now=10.0)
        receipt = self.budget.commit("q1", executed_at=10.0, now=10.0 + 86400 + 60)
        self.assertTrue(receipt.late)
        self.assertEqual(receipt.execution_day, "1970-01-01")
        self.assertEqual(receipt.receipt_day, "1970-01-02")
        # Charged to the day the request ran, not the day the receipt arrived.
        self.assertEqual(self.budget.summary(day="1970-01-01")["used"], 1.0)
        self.assertEqual(self.budget.summary(day="1970-01-02")["used"], 0.0)

    def test_duplicate_receipt_is_not_double_charged(self):
        self.budget.reserve("q1", 1, now=10.0)
        self.budget.commit("q1", now=20.0)
        again = self.budget.commit("q1", now=30.0)
        self.assertFalse(again.accepted)
        self.assertTrue(again.duplicate)
        self.assertEqual(self.budget.summary(day="1970-01-01")["used"], 1.0)

    def test_overage_is_capped_at_headroom(self):
        self.budget.reserve("q1", 1, now=10.0)
        self.budget.reserve("q2", 1, now=10.0)
        self.budget.reserve("q3", 1, now=10.0)
        self.budget.reserve("q4", 1, now=10.0)
        receipt = self.budget.commit("q1", actual_cost=5, now=20.0)
        self.assertEqual(receipt.charged, 2.0)   # reserved 1 + 1 headroom
        self.assertEqual(receipt.overage, 3.0)
        self.assertEqual(self.budget.summary(day="1970-01-01")["used"], 5.0)

    def test_refund_on_lower_actual_cost(self):
        self.budget.reserve("q1", 2, now=10.0)
        receipt = self.budget.commit("q1", actual_cost=1, now=20.0)
        self.assertEqual(receipt.refunded, 1.0)
        self.assertEqual(self.budget.summary(day="1970-01-01")["used"], 1.0)

    def test_unreserved_receipt_is_recorded(self):
        receipt = self.budget.commit("ghost", actual_cost=2, now=20.0)
        self.assertEqual(receipt.reason, "unreserved_receipt")
        self.assertEqual(self.budget.summary(day="1970-01-01")["used"], 2.0)


class ClockAndExpiryTests(unittest.TestCase):
    def test_clock_rollback_does_not_reopen_the_day(self):
        state, time = make_state()
        budget = Budget(state, limits=1)
        self.assertTrue(budget.reserve("q1", 1, now=100.0).granted)
        time.set(50.0)      # wall clock corrected backwards
        # The logical clock keeps the high-water mark, so the same day holds.
        self.assertEqual(budget.today(), "1970-01-01")
        self.assertEqual(budget.summary()["used"], 1.0)
        self.assertFalse(budget.reserve("q2", 1).granted)

    def test_reservation_expiry_refunds_waiting_only(self):
        state, _ = make_state()
        budget = Budget(state, limits=5, reservation_ttl=10)
        budget.reserve("waiting", 1, now=100.0)
        budget.reserve("flying", 1, now=100.0)
        budget.mark_in_flight("flying", now=100.0)
        result = budget.expire(now=120.0)
        self.assertEqual(result["released"], ["waiting"])
        self.assertEqual(result["unknown"], ["flying"])
        self.assertEqual(budget.summary(now=120.0)["used"], 1.0)

    def test_re_reserve_after_release_is_allowed(self):
        state, _ = make_state()
        budget = Budget(state, limits=5)
        budget.reserve("q1", 1, now=100.0)
        budget.release("q1", now=101.0)
        again = budget.reserve("q1", 1, now=102.0)
        self.assertTrue(again.granted)
        self.assertFalse(again.duplicate)


if __name__ == "__main__":
    unittest.main()
