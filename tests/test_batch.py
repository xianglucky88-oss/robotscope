import unittest

from tests.support import make_state, urlset
from robotscope.planner import Planner


ROBOTS = "User-agent: *\nAllow: /\n"


def fetcher_factory(pages, robots=ROBOTS, sitemap=None):
    def fetch(url):
        if url.endswith("/robots.txt"):
            return (200, robots)
        if url.endswith("sitemap.xml") and sitemap is not None:
            return (200, sitemap)
        if url in pages:
            value = pages[url]
            if isinstance(value, BaseException):
                raise value
            return value
        return (404, "")
    return fetch


class BatchDriverTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.planner = Planner(self.state, crawl_id="run", user_agent="ua", limits=10,
                               min_interval=0, global_slots=2, host_slots=1,
                               lease_ttl=100, rules={"default": "deny", "allow": ["/*"]})
        self.sitemap = urlset("https://a.example/1", "https://a.example/2")
        # Seed through discovery + admission with a clean robots fetch.
        self.planner.discover(["https://a.example/sitemap.xml"],
                              fetcher=fetcher_factory({}, sitemap=self.sitemap), now=0.0)
        self.planner.assess(robots_fetcher=fetcher_factory({}, sitemap=self.sitemap),
                            now=0.0, refresh_robots=True)

    def test_batch_executes_and_charges(self):
        pages = {"https://a.example/1": (200, "<html>1</html>"),
                 "https://a.example/2": (200, "<html>2</html>")}
        result = self.planner.run_batch(fetcher_factory(pages), now=0.0)
        # host_slots=1 is re-planned between passes, so both pages run.
        self.assertEqual(result["counts"]["executed"], 2)
        self.assertEqual(result["counts"]["deferred"], 0)
        self.assertEqual(self.planner.budget.summary(now=0.0)["used"], 2.0)
        self.assertEqual(self.planner.leases.slot_usage()["global"], 0)
        self.assertEqual(sorted(i.state for i in self.planner.items()),
                         ["executed", "executed"])

    def test_429_feedback_puts_host_in_wait(self):
        pages = {"https://a.example/1": (429, "", {"retry-after": "120"}),
                 "https://a.example/2": (200, "ok")}
        result = self.planner.run_batch(fetcher_factory(pages), now=0.0)
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["counts"]["executed"], 1)
        host = self.planner.politeness.state_of("a.example")
        self.assertEqual(host.wait_until, 120.0)
        self.assertEqual(host.wait_reason, "http_429")
        # The failed request was charged (it reached the origin) and retried
        # later under a new attempt key; the successful one is charged once.
        self.assertEqual(self.planner.budget.summary(day="1970-01-01")["used"], 2.0)
        plan = self.planner.plan(now=1.0)
        self.assertEqual(plan["counts"]["ready"], 0)
        self.assertEqual({row["reason"] for row in plan["buckets"]["waiting_host"]},
                         {"http_429"})

    def test_server_error_is_retryable_and_charged(self):
        pages = {"https://a.example/1": (503, ""),
                 "https://a.example/2": (200, "ok")}
        result = self.planner.run_batch(fetcher_factory(pages), now=0.0)
        self.assertGreaterEqual(result["counts"]["failed"], 1)
        items = {i.url: i for i in self.planner.items()}
        self.assertEqual(items["https://a.example/1"].state, "discovered")
        self.assertGreaterEqual(items["https://a.example/1"].attempt, 2)
        self.assertGreaterEqual(self.planner.budget.summary(day="1970-01-01")["used"], 1.0)
        # The failure did not hot-loop: it was attempted once this run.
        self.assertEqual(
            sum(1 for row in result["failed"] if row["url"] == "https://a.example/1"), 1)

    def test_lease_is_revalidated_before_sending(self):
        # Simulate a worker whose lease expired between dispatch and send.
        original = self.planner.may_execute
        self.planner.may_execute = lambda *a, **k: False
        try:
            result = self.planner.run_batch(fetcher_factory({}), now=0.0)
        finally:
            self.planner.may_execute = original
        self.assertEqual(result["counts"]["executed"], 0)
        stale = [row for row in result["deferred"] if row["reason"] == "lease_stale"]
        self.assertEqual(len(stale), 1)
        # The dispatch did reserve quota; recovery resolves the unknown later.
        self.assertEqual(self.planner.leases.slot_usage()["global"], 1)

    def test_fetch_exception_marks_failure(self):
        pages = {"https://a.example/1": RuntimeError("connection reset"),
                 "https://a.example/2": (200, "ok")}
        result = self.planner.run_batch(fetcher_factory(pages), now=0.0)
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertIn("fetch_error", result["failed"][0]["reason"])
        self.assertEqual(self.planner.budget.summary(day="1970-01-01")["used"], 2.0)


if __name__ == "__main__":
    unittest.main()
