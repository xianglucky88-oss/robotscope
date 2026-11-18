import unittest

from tests.support import make_state
from robotscope import urls
from robotscope.compat import parse_input
from robotscope.planner import Planner


class NormalisationTests(unittest.TestCase):
    def test_case_default_port_and_fragment(self):
        self.assertEqual(urls.normalize("HTTP://Example.COM:80/a#frag"), "http://example.com/a")
        self.assertEqual(urls.normalize("https://example.com:443/a"), "https://example.com/a")
        self.assertEqual(urls.normalize("https://example.com:8443/a"),
                         "https://example.com:8443/a")

    def test_dot_segments_and_trailing_slash(self):
        self.assertEqual(urls.normalize("https://x/a/./b/../c"), "https://x/a/c")
        self.assertEqual(urls.normalize("https://x/a/"), "https://x/a/")

    def test_query_order_is_canonical(self):
        self.assertEqual(urls.normalize("https://x/a?b=2&a=1"), "https://x/a?a=1&b=2")

    def test_identity_helpers(self):
        self.assertEqual(urls.host_of("https://x:8443/a"), "x:8443")
        self.assertEqual(urls.host_of("https://X/a"), "x")
        self.assertEqual(urls.path_of("https://x/a?b=1"), "/a?b=1")
        self.assertEqual(urls.origin_of("https://x/a"), "https://x")

    def test_unsupported_schemes_rejected(self):
        for bad in ("mailto:a@b", "ftp://x/a", "", "not-a-url"):
            with self.assertRaises(urls.URLError):
                urls.normalize(bad)


class CompatTests(unittest.TestCase):
    def test_legacy_list_is_not_turned_into_urls(self):
        request = parse_input([{"id": "r-1", "src": "a", "value": 1}])
        self.assertEqual(request.urls, [])
        self.assertEqual(len(request.legacy), 1)

    def test_structured_mapping(self):
        request = parse_input({
            "crawl_id": "site",
            "user_agent": "bot",
            "sitemap": "https://x/s.xml",
            "urls": ["https://x/a"],
            "limits": 10,
            "priority": {"x": 2},
            "history": {"x": 3},
            "deadlines": {"https://x/a": "2026-12-01T00:00:00Z"},
            "unknown_field": 5,
        })
        self.assertEqual(request.crawl_id, "site")
        self.assertEqual(request.sitemaps, ["https://x/s.xml"])
        self.assertEqual(request.priority_for("https://x/a"), 2.0)
        self.assertEqual(request.history_for("https://x/a"), 3.0)
        self.assertIsNotNone(request.deadline_for("https://x/a"))
        self.assertEqual(request.extra["unknown_field"], 5)

    def test_wrapped_mapping(self):
        request = parse_input({"crawl": {"crawl_id": "w", "urls": ["https://x/a"]}})
        self.assertEqual(request.crawl_id, "w")
        self.assertEqual(request.urls, ["https://x/a"])

    def test_planner_from_legacy_input_never_invents_permission(self):
        state, _ = make_state()
        planner = Planner(state, limits=5, rules={"default": "deny", "allow": ["/*"]})
        from robotscope.compat import plan_from_input
        result = plan_from_input(planner, [{"id": "r-1", "value": 3}], now=100.0)
        self.assertEqual(result["request"].legacy[0]["id"], "r-1")
        self.assertEqual(result["plan"]["counts"]["known"], 0)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.planner = Planner(self.state, limits=10, min_interval=0,
                               rules={"default": "deny", "allow": ["/*"]})

    def test_report_contains_coverage_pace_quota_and_deferrals(self):
        report = self.planner.report(now=self.time())
        for key in ("coverage", "pace", "budget", "deferrals", "counts", "policy",
                    "policy_versions", "robots_versions"):
            self.assertIn(key, report)
        self.assertEqual(report["coverage"]["deletion_inference"], "blocked_incomplete_scan")
        self.assertIn("remaining", report["budget"])

    def test_explain_returns_full_trail(self):
        def fetch(url):
            return (200, "User-agent: *\nAllow: /\n") if url.endswith("robots.txt") else (404, "")
        self.planner.add_url("https://a.example/one", now=self.time())
        self.planner.assess(robots_fetcher=fetch, now=self.time(), refresh_robots=True)
        rid = self.planner.plan(now=self.time())["batch"][0]["request_id"]
        dispatched = self.planner.dispatch(rid, now=self.time())
        self.planner.complete(rid, lease_id=dispatched.lease.lease_id,
                              fence=dispatched.lease.fence, now=self.time())
        trail = self.planner.explain(rid)
        self.assertEqual(trail["item"]["state"], "executed")
        self.assertEqual(trail["ledger"]["state"], "spent")
        self.assertGreaterEqual(len(trail["attempts"]), 1)
        kinds = {e["kind"] for e in trail["events"]}
        self.assertIn("dispatch_granted", kinds)
        self.assertIn("request_completed", kinds)


if __name__ == "__main__":
    unittest.main()
