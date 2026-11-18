import unittest

from tests.support import FakeFetcher, make_state
from robotscope.robots import RobotsRules, RobotsStore, parse_robots


class ParsingTests(unittest.TestCase):
    def test_group_selection_most_specific(self):
        rules = parse_robots(
            "User-agent: *\nDisallow: /\n\n"
            "User-agent: robotscope\nDisallow: /private\n")
        self.assertTrue(rules.decide("/public", "robotscope/1.0").allowed)
        self.assertFalse(rules.decide("/private", "robotscope/1.0").allowed)
        self.assertFalse(rules.decide("/anything", "otherbot").allowed)

    def test_longest_match_and_allow_tie(self):
        rules = parse_robots(
            "User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /a/b/c\n")
        self.assertFalse(rules.decide("/a", "x").allowed)
        self.assertTrue(rules.decide("/a/b", "x").allowed)
        self.assertFalse(rules.decide("/a/b/c", "x").allowed)

    def test_empty_disallow_is_no_restriction(self):
        rules = parse_robots("User-agent: *\nDisallow:\n")
        self.assertTrue(rules.decide("/anything", "x").allowed)

    def test_wildcard_and_end_anchor(self):
        rules = parse_robots("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*\n")
        self.assertFalse(rules.decide("/files/doc.pdf", "x").allowed)
        self.assertTrue(rules.decide("/files/doc.pdf?x=1", "x").allowed)
        self.assertFalse(rules.decide("/tmp/1", "x").allowed)

    def test_crawl_delay_and_sitemaps(self):
        rules = parse_robots(
            "User-agent: *\nCrawl-delay: 2.5\nSitemap: https://x/s.xml\n")
        self.assertEqual(rules.delay_for("any"), 2.5)
        self.assertEqual(rules.sitemaps, ["https://x/s.xml"])

    def test_evidence_has_line_numbers(self):
        rules = parse_robots("User-agent: *\nDisallow: /x\n")
        match = rules.decide("/x/y", "ua")
        self.assertEqual(match.line, 2)
        self.assertEqual(match.pattern, "/x")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.store = RobotsStore(self.state, ttl=100, backoff=50)

    def test_fresh_fetch_allows_and_versions(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nDisallow: /no\n")})
        ev = self.store.check("https://a/ok", "ua", now=10.0, refresh=True, fetcher=fetcher)
        self.assertTrue(ev.allowed)
        self.assertEqual(ev.reason, "no_matching_rule")
        self.assertTrue(ev.version.startswith("r1:"))
        self.assertEqual(ev.expires_at, 110.0)

    def test_denial_carries_version_and_rule(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nDisallow: /no\n")})
        ev = self.store.check("https://a/no/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.matched.pattern, "/no")
        self.assertEqual(ev.matched.line, 2)

    def test_missing_cache_denies_without_fetch(self):
        ev = self.store.check("https://a/x", "ua", now=10.0)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.reason, "cache_missing")

    def test_404_is_absent_but_still_versioned(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (404, "")})
        ev = self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        self.assertTrue(ev.allowed)
        self.assertEqual(ev.reason, "robots_absent")
        self.assertTrue(ev.version)

    def test_expired_cache_with_failed_refresh_denies(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nAllow: /\n")})
        self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        boom = FakeFetcher({"https://a/robots.txt": OSError("dns")})
        ev = self.store.check("https://a/x", "ua", now=200.0, refresh=True, fetcher=boom)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.reason, "expired_refresh_failed")
        self.assertTrue(ev.stale)

    def test_expired_allow_without_refresh_is_not_permission(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nAllow: /\n")})
        self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        ev = self.store.check("https://a/x", "ua", now=500.0)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.reason, "stale_unverified")

    def test_expired_disallow_still_denies(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nDisallow: /x\n")})
        self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        ev = self.store.check("https://a/x", "ua", now=500.0)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.reason, "stale_disallowed")

    def test_http_error_is_not_permission(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (500, "err")})
        ev = self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        self.assertFalse(ev.allowed)
        self.assertEqual(ev.reason, "fetch_failed")

    def test_backoff_prevents_hammering(self):
        boom = FakeFetcher({"https://a/robots.txt": OSError("dns")})
        self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=boom)
        again = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nAllow: /\n")})
        ev = self.store.check("https://a/x", "ua", now=20.0, refresh=True, fetcher=again)
        self.assertFalse(ev.allowed)
        self.assertEqual(again.calls, [])  # no fetch during backoff

    def test_new_version_supersedes_old(self):
        fetcher = FakeFetcher({"https://a/robots.txt": (200, "User-agent: *\nAllow: /\n")})
        first = self.store.check("https://a/x", "ua", now=10.0, refresh=True, fetcher=fetcher)
        fetcher.set("https://a/robots.txt", (200, "User-agent: *\nDisallow: /\n"))
        second = self.store.check("https://a/x", "ua", now=200.0, refresh=True, fetcher=fetcher)
        self.assertNotEqual(first.version, second.version)
        self.assertFalse(second.allowed)
        versions = self.store.versions("a", "ua")
        self.assertEqual(len(versions), 2)

    def test_user_agent_specific_denial(self):
        text = "User-agent: *\nAllow: /\n\nUser-agent: badbot\nDisallow: /\n"
        fetcher = FakeFetcher({"https://a/robots.txt": (200, text)})
        good = self.store.check("https://a/x", "goodbot", now=10.0, refresh=True, fetcher=fetcher)
        self.assertTrue(good.allowed)
        bad = self.store.check("https://a/x", "badbot/2.0", now=10.0, refresh=True, fetcher=fetcher)
        self.assertFalse(bad.allowed)
        self.assertEqual(bad.matched.group, "badbot")


if __name__ == "__main__":
    unittest.main()
