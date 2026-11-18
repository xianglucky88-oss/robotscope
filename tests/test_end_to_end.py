import json
import unittest

from tests.support import make_state, urlset
from robotscope.fetch import FetchError, OfflineFetcher
from robotscope.planner import Planner
from robotscope.state import State


ROUTES = {
    "https://shop.example/sitemap.xml": urlset(
        "https://shop.example/products/a",
        "https://shop.example/products/b",
        "https://shop.example/admin/x",
    ),
    "https://shop.example/robots.txt": (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Crawl-delay: 2\n"
        "Sitemap: https://shop.example/sitemap.xml\n"
    ),
}


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.planner = Planner(
            self.state,
            crawl_id="shop",
            user_agent="robotscope",
            limits=1,
            min_interval=0,
            global_slots=1,
            lease_ttl=600,
            rules={"default": "deny", "allow": ["/*"]},
        )

    def test_offline_flow(self):
        fetcher = OfflineFetcher(ROUTES)
        discovery = self.planner.discover(
            ["https://shop.example/sitemap.xml"], fetcher=fetcher, now=1000.0)
        self.assertTrue(discovery["complete"])
        self.assertEqual(discovery["added"], 3)
        self.planner.assess(robots_fetcher=fetcher, now=1000.0, refresh_robots=True)

        plan = self.planner.plan(now=1000.0)
        self.assertEqual(plan["counts"]["known"], 3)
        self.assertEqual(plan["counts"]["denied"], 1)
        self.assertEqual(plan["buckets"]["denied"][0]["url"],
                         "https://shop.example/admin/x")
        # Crawl-delay from robots is applied to pacing.
        self.assertEqual(plan["pace"]["hosts"]["shop.example"]["interval"], 2.0)

        first = plan["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(first, now=1000.0)
        self.assertTrue(dispatched.granted)
        # Second URL on the same host is paced and the single slot is busy.
        plan = self.planner.plan(now=1000.0)
        self.assertGreaterEqual(plan["counts"]["waiting_host"] + plan["counts"]["waiting_slot"], 1)

        self.planner.complete(first, lease_id=dispatched.lease.lease_id,
                              fence=dispatched.lease.fence, now=1000.5)
        plan = self.planner.plan(now=1003.0)
        # Quota (limit=1) is spent, so the remaining URL waits for reset.
        self.assertEqual(plan["counts"]["ready"], 0)
        self.assertEqual(plan["counts"]["waiting_budget"], 1)
        self.assertEqual(plan["remaining_after_budget"],
                         ["https://shop.example/products/a"])

        report = self.planner.report(now=1003.0)
        self.assertEqual(report["coverage"]["discovered"], 3)
        self.assertEqual(report["budget"]["used"], 1.0)
        self.assertEqual(report["coverage"]["denial_reasons"], {"disallowed": 1})
        self.assertEqual(report["pace"]["hosts"]["shop.example"]["next_allowed_at"], 1002.0)

    def test_robots_failure_defers_everything_on_that_host(self):
        fetcher = OfflineFetcher({"https://r.example/robots.txt": FetchError("dns down")})
        self.planner.add_url("https://r.example/a", now=500.0)
        self.planner.assess(robots_fetcher=fetcher, now=500.0, refresh_robots=True)
        plan = self.planner.plan(now=500.0)
        self.assertEqual(plan["counts"]["ready"], 0)
        self.assertEqual(plan["counts"]["denied"], 0)
        self.assertEqual(plan["counts"]["unassessed"], 1)
        self.assertEqual(plan["buckets"]["unassessed"][0]["reason"], "fetch_failed")
        self.assertEqual(plan["budget"]["used"], 0.0)

    def test_offline_fixture_from_file(self):
        import tempfile
        import os
        path = os.path.join(tempfile.mkdtemp(), "routes.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(ROUTES, handle)
        fetcher = OfflineFetcher.from_file(path)
        self.assertEqual(fetcher("https://shop.example/robots.txt")[0], 200)


if __name__ == "__main__":
    unittest.main()
