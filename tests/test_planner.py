import json
import os
import tempfile
import unittest

from tests.support import FakeTime, make_state, sitemapindex, urlset
from robotscope.clock import Clock
from robotscope.planner import Planner, request_id_for
from robotscope.state import State


ROBOTS = "User-agent: *\nDisallow: /private\n"


def full_fetcher(routes):
    def fetch(url):
        if url in routes:
            value = routes[url]
            if isinstance(value, BaseException):
                raise value
            return (200, value)
        if url.endswith("/robots.txt"):
            return (200, ROBOTS)
        return (404, "")
    return fetch


class PlannerHarness(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.planner = Planner(self.state, crawl_id="t", user_agent="ua",
                               limits=10, min_interval=0,
                               global_slots=4, host_slots=1,
                               lease_ttl=100,
                               rules={"default": "deny", "allow": ["/*"]})

    def seed(self, *urls, fetch=None):
        routes = {"https://a.example/sitemap.xml": urlset(*urls)}
        fetch = fetch or full_fetcher(routes)
        self.planner.discover(["https://a.example/sitemap.xml"], fetcher=fetch, now=self.time())
        self.planner.assess(robots_fetcher=fetch, now=self.time(), refresh_robots=True)

    def rid(self, url):
        return request_id_for("t", url)

    def plan(self, **kwargs):
        return self.planner.plan(now=self.time(), **kwargs)


class DiscoveryAdmissionTests(PlannerHarness):
    def test_buckets_account_for_every_url(self):
        self.seed("https://a.example/one", "https://a.example/private/x", "https://a.example/two")
        plan = self.plan()
        self.assertEqual(plan["counts"]["known"], 3)
        self.assertEqual(plan["counts"]["denied"], 1)
        bucket_total = sum(plan["counts"][k] for k in (
            "ready", "waiting_host", "waiting_budget", "waiting_slot",
            "unassessed", "denied", "invalidated", "in_flight", "finished"))
        self.assertEqual(bucket_total, plan["counts"]["known"])
        denied_urls = [row["url"] for row in plan["buckets"]["denied"]]
        self.assertEqual(denied_urls, ["https://a.example/private/x"])

    def test_incomplete_discovery_blocks_deletion_claims(self):
        routes = {"https://a.example/sitemap.xml": sitemapindex(
            "https://a.example/a.xml", "https://a.example/b.xml"),
            "https://a.example/a.xml": urlset("https://a.example/one"),
            "https://a.example/b.xml": RuntimeError("timeout")}
        result = self.planner.discover(["https://a.example/sitemap.xml"],
                                       fetcher=full_fetcher(routes), now=self.time())
        self.assertFalse(result["complete"])
        self.assertEqual(result["deletions"], [])
        plan = self.plan()
        self.assertEqual(plan["coverage"]["deletion_inference"], "blocked_incomplete_scan")

    def test_robots_missing_defers_instead_of_allowing(self):
        self.planner.add_url("https://no-robots.example/x", now=self.time())
        # assess without a working robots fetch -> cache_missing
        self.planner.assess(robots_fetcher=lambda u: (_ for _ in ()).throw(OSError("dns")),
                            now=self.time(), refresh_robots=True)
        plan = self.plan()
        self.assertEqual(plan["counts"]["denied"], 0)
        self.assertEqual(plan["counts"]["unassessed"], 1)
        self.assertEqual(plan["counts"]["ready"], 0)
        self.assertEqual(plan["buckets"]["unassessed"][0]["reason"], "fetch_failed")
        self.assertEqual(plan["budget"]["used"], 0.0)
        # Dispatch remains refused; no quota or slot is consumed.
        item = self.planner.items()[0]
        result = self.planner.dispatch(item.request_id, now=self.time())
        self.assertFalse(result.granted)
        self.assertEqual(self.planner.budget.summary(now=self.time())["used"], 0.0)
        self.assertEqual(self.planner.leases.slot_usage()["global"], 0)

    def test_priority_cannot_override_robots(self):
        self.seed("https://a.example/private/x")
        item = self.planner.items()[0]
        self.assertEqual(item.state, "denied")
        result = self.planner.dispatch(item.request_id, now=self.time())
        self.assertFalse(result.granted)
        self.assertTrue(result.reason.startswith("denied"))


class DispatchTests(PlannerHarness):
    def test_dispatch_reserves_budget_and_slot(self):
        self.seed("https://a.example/one")
        plan = self.plan()
        rid = plan["batch"][0]["request_id"]
        result = self.planner.dispatch(rid, now=self.time())
        self.assertTrue(result.granted)
        self.assertEqual(self.planner.budget.summary(now=self.time())["in_flight"], 1.0)
        self.assertEqual(self.planner.leases.slot_usage()["global"], 1)

    def test_duplicate_dispatch_is_idempotent(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        first = self.planner.dispatch(rid, now=self.time())
        again = self.planner.dispatch(rid, now=self.time())
        self.assertTrue(again.granted)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.lease.lease_id, first.lease.lease_id)
        summary = self.planner.budget.summary(now=self.time())
        self.assertEqual(summary["used"], 1.0)
        self.assertEqual(self.planner.leases.slot_usage()["global"], 1)

    def test_host_wait_defers_without_taking_a_slot(self):
        self.seed("https://a.example/one", "https://a.example/two")
        by_url = {item.url: item for item in self.planner.items()}
        first = by_url["https://a.example/one"]
        second = by_url["https://a.example/two"]
        self.planner.dispatch(first.request_id, now=self.time())
        self.planner.report_host("a.example", retry_after=60, status=429, now=self.time())
        result = self.planner.dispatch(second.request_id, now=self.time())
        self.assertFalse(result.granted)
        self.assertTrue(result.reason.startswith("host_wait"))
        self.assertEqual(self.planner.leases.slot_usage()["global"], 1)
        self.assertEqual(self.planner.budget.reservation_count(state="reserved"), 0)
        plan = self.plan()
        self.assertEqual([r["url"] for r in plan["buckets"]["waiting_host"]],
                         ["https://a.example/two"])

    def test_budget_exhaustion_reports_remaining(self):
        planner = Planner(self.state, crawl_id="b", user_agent="ua", limits=1,
                          rules={"default": "deny", "allow": ["/*"]})
        planner.add_url("https://a.example/one", now=self.time())
        planner.add_url("https://a.example/two", now=self.time())
        planner.assess(robots_fetcher=full_fetcher({}), now=self.time(), refresh_robots=True)
        plan = planner.plan(now=self.time())
        self.assertEqual(plan["counts"]["ready"], 1)
        self.assertEqual(plan["counts"]["waiting_budget"], 1)
        self.assertEqual(plan["remaining_after_budget"], ["https://a.example/two"])
        retry_at = plan["buckets"]["waiting_budget"][0]["retry_at"]
        self.assertEqual(retry_at, 86400.0)

    def test_slot_limit_defers_without_spending_budget(self):
        planner = Planner(self.state, crawl_id="s", user_agent="ua", limits=10,
                          global_slots=1, host_slots=1,
                          rules={"default": "deny", "allow": ["/*"]})
        planner.add_url("https://a.example/one", now=self.time())
        planner.add_url("https://b.example/two", now=self.time())
        planner.assess(robots_fetcher=full_fetcher({}), now=self.time(), refresh_robots=True)
        plan = planner.plan(now=self.time())
        self.assertEqual(plan["counts"]["waiting_slot"], 1)
        self.assertEqual(plan["budget"]["used"], 0.0)


class CompletionTests(PlannerHarness):
    def test_complete_releases_slot_and_charges(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(rid, now=self.time())
        receipt = self.planner.complete(rid, lease_id=dispatched.lease.lease_id,
                                        fence=dispatched.lease.fence, now=self.time())
        self.assertTrue(receipt["lease_valid"])
        self.assertEqual(receipt["state"], "spent")
        self.assertEqual(self.planner.leases.slot_usage()["global"], 0)
        item = self.planner.items()[0]
        self.assertEqual(item.state, "executed")

    def test_stale_worker_receipt_is_flagged_but_charged(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(rid, now=self.time(), ttl=10)
        self.time.advance(50)
        self.planner.recover(now=self.time())
        receipt = self.planner.complete(rid, lease_id=dispatched.lease.lease_id,
                                        fence=dispatched.lease.fence, now=self.time())
        self.assertFalse(receipt["lease_valid"])
        self.assertEqual(receipt["state"], "spent")
        self.assertEqual(self.planner.budget.summary(now=self.time())["used"], 1.0)

    def test_retry_charges_a_new_attempt(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(rid, now=self.time())
        self.planner.fail(rid, lease_id=dispatched.lease.lease_id, fence=dispatched.lease.fence,
                          now=self.time())
        self.time.advance(1)
        dispatched2 = self.planner.dispatch(rid, now=self.time())
        self.assertTrue(dispatched2.granted)
        self.assertEqual(dispatched2.lease.fence, dispatched.lease.fence + 1)
        self.assertEqual(self.planner.budget.summary(now=self.time())["used"], 2.0)

    def test_late_receipt_after_day_rollover(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        executed_at = self.time()
        dispatched = self.planner.dispatch(rid, now=executed_at)
        self.time.advance(86400 + 30)
        receipt = self.planner.complete(rid, lease_id=dispatched.lease.lease_id,
                                        fence=dispatched.lease.fence,
                                        executed_at=executed_at, now=self.time())
        self.assertTrue(receipt["late"])
        self.assertEqual(receipt["receipt"]["execution_day"], "1970-01-01")
        self.assertEqual(receipt["receipt"]["receipt_day"], "1970-01-02")
        self.assertEqual(self.planner.budget.summary(day="1970-01-01")["used"], 1.0)
        self.assertEqual(self.planner.budget.summary(day="1970-01-02")["used"], 0.0)


class PolicyInvalidationTests(PlannerHarness):
    def test_pending_work_is_released_when_rules_change(self):
        self.seed("https://a.example/docs/a", "https://a.example/docs/b")
        by_url = {item.url: item for item in self.planner.items()}
        first = by_url["https://a.example/docs/a"]
        other = by_url["https://a.example/docs/b"]
        self.planner.dispatch(first.request_id, now=self.time())
        # Reserve quota for the other request without dispatching it.
        self.planner.budget.reserve(other.budget_key(), other.cost, host=other.host,
                                    now=self.time())
        self.planner.politeness.enqueue(other.host, other.request_id, other.url, now=self.time())
        rid, other_id = first.request_id, other.request_id
        result = self.planner.apply_policy(
            {"default": "deny", "disallow": ["/docs/*"]}, now=self.time())
        self.assertEqual(len(result["invalidated"]), 1)
        self.assertEqual([entry["request_id"] for entry in result["invalidated"]], [other_id])
        not_refunded = {entry["request_id"]: entry for entry in result["not_refunded"]}
        self.assertIn(rid, not_refunded)
        self.assertTrue(not_refunded[rid]["newly_denied"])
        self.assertEqual(not_refunded[rid]["reason"], "executed_or_unknown_not_refundable")
        released = {entry["request_id"]: entry for entry in result["released"]}
        self.assertTrue(released[other_id]["released"])
        self.assertEqual(released[other_id]["refunded"], 1.0)
        self.assertTrue(released[other_id]["dequeued"])
        self.assertEqual(self.planner.politeness.queue(), [])
        self.assertEqual(self.planner.budget.summary(now=self.time())["used"], 1.0)

    def test_executed_work_is_not_refunded(self):
        self.seed("https://a.example/docs/a")
        rid = self.plan()["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(rid, now=self.time())
        self.planner.complete(rid, lease_id=dispatched.lease.lease_id,
                              fence=dispatched.lease.fence, now=self.time())
        result = self.planner.apply_policy({"default": "deny"}, now=self.time())
        self.assertEqual(result["invalidated"], [])
        self.assertEqual(self.planner.budget.summary(now=self.time())["used"], 1.0)


class RecoveryTests(PlannerHarness):
    def test_recover_reclaims_lease_and_marks_unknown(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        self.planner.dispatch(rid, now=self.time(), ttl=10)
        self.time.advance(60)
        result = self.planner.recover(now=self.time())
        self.assertEqual(len(result["reclaimed_leases"]), 1)
        self.assertEqual(result["unknown_requests"], [rid])
        self.assertEqual(self.planner.leases.slot_usage()["global"], 0)
        self.assertEqual(self.planner.budget.summary(now=self.time())["unknown"], 1.0)
        self.assertEqual([i.state for i in self.planner.items()], ["unknown"])

    def test_clock_rollback_does_not_reopen_budget_day(self):
        self.seed("https://a.example/one")
        rid = self.plan()["batch"][0]["request_id"]
        self.planner.dispatch(rid, now=self.time())
        day = self.planner.budget.today()
        self.time.set(self.time() - 3600)
        self.assertEqual(self.planner.budget.today(), day)
        self.assertEqual(self.planner.budget.summary()["used"], 1.0)

    def test_restart_preserves_policy_plan_and_ledger(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "state.db")
        clock = Clock(wall=FakeTime(1000.0))
        state = State(path, clock=clock)
        planner = Planner(state, crawl_id="r", user_agent="ua", limits=5,
                          rules={"default": "deny", "allow": ["/*"]})
        planner.add_url("https://a.example/one", now=1000.0)
        planner.assess(robots_fetcher=full_fetcher({}), now=1000.0, refresh_robots=True)
        rid = planner.plan(now=1000.0)["batch"][0]["request_id"]
        dispatched = planner.dispatch(rid, now=1000.0)
        policy_version = planner.policy.active.version
        state.close()

        # A new process reads the same database.
        clock2 = Clock(wall=FakeTime(1000.0))
        state2 = State(path, clock=clock2)
        planner2 = Planner(state2, crawl_id="r", user_agent="ua", limits=5,
                           rules={"default": "deny", "allow": ["/*"]})
        self.assertEqual(planner2.policy.active.version, policy_version)
        self.assertEqual(planner2.budget.summary(now=1000.0)["used"], 1.0)
        self.assertEqual(len(planner2.items()), 1)
        self.assertEqual(planner2.items()[0].state, "leased")
        self.assertTrue(planner2.may_execute(rid, dispatched.lease.lease_id,
                                             fence=dispatched.lease.fence, now=1000.0))
        state2.close()

    def test_restart_after_clock_rollback_keeps_high_water(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "state.db")
        state = State(path, clock=Clock(wall=FakeTime(5000.0)))
        state.clock.now()
        state.close()
        state2 = State(path, clock=Clock(wall=FakeTime(100.0)))
        self.assertGreaterEqual(state2.clock.now(), 5000.0)
        state2.close()


if __name__ == "__main__":
    unittest.main()
