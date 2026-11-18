import unittest

from tests.support import make_state
from robotscope.politeness import Politeness


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.polite = Politeness(self.state, default_interval=10)

    def test_requests_are_spaced_per_host(self):
        self.polite.note_request("a", now=100.0)
        self.assertEqual(self.polite.next_allowed("a"), 110.0)
        self.assertEqual(self.polite.next_allowed("b"), 0.0)  # hosts are independent

    def test_crawl_delay_wins_when_larger(self):
        self.polite.note_request("a", now=100.0, crawl_delay=30)
        self.assertEqual(self.polite.next_allowed("a"), 130.0)
        self.polite.note_request("b", now=100.0, crawl_delay=2)
        self.assertEqual(self.polite.next_allowed("b"), 110.0)  # default still applies

    def test_host_wait_is_separate_from_pacing(self):
        self.polite.note_request("a", now=100.0)
        self.polite.impose_wait("a", 120, reason="http_429", now=105.0)
        st = self.polite.state_of("a")
        self.assertEqual(st.wait_until, 225.0)
        self.assertEqual(st.next_allowed_at, 110.0)
        metric = self.polite.metric(now=106.0)["hosts"]["a"]
        self.assertTrue(metric["waiting"])
        self.assertEqual(metric["pacing_ready_in"], 4.0)
        self.assertEqual(metric["wait_remaining"], 119.0)

    def test_wait_never_shortens(self):
        self.polite.impose_wait("a", 100, now=100.0)
        self.polite.impose_wait("a", 10, now=110.0)
        self.assertEqual(self.polite.state_of("a").wait_until, 200.0)

    def test_crawl_delay_from_robots_is_persisted(self):
        self.polite.configure_interval("a", 60, now=100.0)
        self.polite.note_request("a", now=100.0)
        self.assertEqual(self.polite.next_allowed("a"), 160.0)
        # A fresh Politeness over the same state sees the same pacing.
        again = Politeness(self.state, default_interval=10)
        self.assertEqual(again.next_allowed("a"), 160.0)

    def test_queue_is_idempotent(self):
        self.polite.enqueue("a", "q1", "https://a/1", now=100.0)
        self.polite.enqueue("a", "q1", "https://a/1", now=101.0)
        self.assertEqual(len(self.polite.queue()), 1)
        self.assertIsNotNone(self.polite.dequeue("q1"))
        self.assertEqual(self.polite.queue(), [])

    def test_clock_rollback_does_not_erase_wait(self):
        self.polite.impose_wait("a", 100, now=1000.0)
        self.time.set(10.0)
        self.assertEqual(self.polite.state_of("a").wait_until, 1100.0)


class RankingTests(unittest.TestCase):
    def _candidates(self):
        return [
            {"url": "https://a/one", "priority": 0.0},
            {"url": "https://a/two", "priority": 5.0},
            {"url": "https://b/three", "cost": 4.0},
            {"url": "https://b/four", "deadline": 1_000.0},
        ]

    def test_priority_affects_order(self):
        from robotscope import value_rank
        ranked = value_rank.rank(
            value_rank.candidates_from(self._candidates(), now=100.0), now=100.0)
        self.assertEqual(ranked[0].candidate.url, "https://a/two")
        self.assertEqual([r.rank for r in ranked], [1, 2, 3, 4])

    def test_higher_cost_ranks_lower_all_else_equal(self):
        from robotscope import value_rank
        ranked = value_rank.rank(value_rank.candidates_from([
            {"url": "https://a/cheap", "cost": 1},
            {"url": "https://a/pricey", "cost": 4},
        ], now=100.0), now=100.0)
        self.assertEqual(ranked[0].candidate.url, "https://a/cheap")

    def test_deadline_adds_urgency(self):
        from robotscope import value_rank
        base = [{"url": "https://a/soon", "deadline": 200.0},
                {"url": "https://a/later", "deadline": 100_000.0}]
        ranked = value_rank.rank(value_rank.candidates_from(base, now=100.0), now=100.0)
        self.assertEqual(ranked[0].candidate.url, "https://a/soon")

    def test_starvation_bonus_grows_with_wait(self):
        from robotscope import value_rank
        fresh = value_rank.score(value_rank.Candidate(url="https://a/x", enqueued_at=1_000.0),
                                 now=1_000.0)
        old = value_rank.score(value_rank.Candidate(url="https://a/x", enqueued_at=0.0),
                               now=1_000.0)
        self.assertGreater(old.breakdown["starvation"], fresh.breakdown["starvation"])
        self.assertGreater(old.score, fresh.score)

    def test_fairness_rotates_within_round(self):
        from robotscope import value_rank
        candidates = value_rank.candidates_from([
            {"url": f"https://big/{i}", "priority": 3.0, "last_served_at": 900.0}
            for i in range(4)
        ] + [
            {"url": "https://small/one", "priority": 0.0, "last_served_at": 10.0},
        ], now=1_000.0)
        ranked = value_rank.rank(candidates, now=1_000.0, round_size=5)
        self.assertEqual(ranked[0].candidate.url, "https://small/one")


if __name__ == "__main__":
    unittest.main()
