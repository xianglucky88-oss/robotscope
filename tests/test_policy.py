import unittest

from tests.support import make_state
from robotscope.policy_engine import PolicyEngine, RuleError, RuleSet
from robotscope.robots import RobotsStore


def evidence(store, url, ua="ua", now=10.0):
    fetcher = lambda u: (200, "User-agent: *\nDisallow: /blocked\n")
    return store.check(url, ua, now=now, refresh=True, fetcher=fetcher)


class RuleSetTests(unittest.TestCase):
    def test_version_is_content_addressed(self):
        a = RuleSet.from_doc({"disallow": ["/a"]})
        b = RuleSet.from_doc({"disallow": ["/a"]})
        c = RuleSet.from_doc({"disallow": ["/b"]})
        self.assertEqual(a.version, b.version)
        self.assertNotEqual(a.version, c.version)

    def test_invalid_documents_are_rejected(self):
        with self.assertRaises(RuleError):
            RuleSet.from_doc({"default": "maybe"})
        with self.assertRaises(RuleError):
            RuleSet.from_doc({"disallow": [123]})
        with self.assertRaises(RuleError):
            RuleSet.from_doc({"max_depth": -1})

    def test_string_shorthand(self):
        rule = RuleSet.from_doc({"allow": "/docs/*"})
        self.assertEqual(rule.doc["allow"], "/docs/*")


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.robots = RobotsStore(self.state, ttl=1000)
        self.engine = PolicyEngine(self.state)
        self.engine.activate({"default": "deny", "allow": ["/docs/*", "/"]})

    def test_robots_denial_is_absolute_even_with_high_priority(self):
        ev = self.robots.check("https://a/blocked", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, "User-agent: *\nDisallow: /blocked\n"))
        decision = self.engine.check("https://a/blocked", user_agent="ua", robots=ev)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "disallowed")
        # A matching path rule cannot override robots.
        engine = PolicyEngine(self.state)
        engine.activate({"default": "allow", "allow": ["/blocked"]})
        self.assertFalse(engine.check("https://a/blocked", user_agent="ua", robots=ev).allowed)

    def test_missing_robots_denies_by_default(self):
        decision = self.engine.check("https://a/docs/x", user_agent="ua", robots=None)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "robots_unavailable")

    def test_stale_robots_denies(self):
        ev = self.robots.check("https://a/docs/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, "User-agent: *\nAllow: /\n"))
        stale = self.robots.check("https://a/docs/x", "ua", now=5000.0)
        self.assertTrue(stale.stale)
        decision = self.engine.check("https://a/docs/x", user_agent="ua", robots=stale)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "robots_stale")

    def test_evidence_for_other_host_or_agent_is_rejected(self):
        ev = self.robots.check("https://a/docs/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, "User-agent: *\nAllow: /\n")).as_dict()
        other_host = {**ev, "host": "b"}
        self.assertFalse(self.engine.check("https://a/docs/x", user_agent="ua",
                                           robots=other_host).allowed)
        other_agent = {**ev, "user_agent": "someone-else"}
        self.assertFalse(self.engine.check("https://a/docs/x", user_agent="ua",
                                           robots=other_agent).allowed)

    def test_path_allowlist_and_denylist(self):
        engine = PolicyEngine(self.state)
        engine.activate({"default": "deny", "allow": ["/docs/*"],
                         "disallow": ["/docs/private/*"]})
        ok = self.robots.check("https://a/docs/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, ""))
        denied = self.engine.check("https://a/docs/x", user_agent="ua", robots=ok)
        self.assertTrue(denied.allowed)
        # A robots verdict for /docs/x says nothing about /docs/private/y.
        private_ev = self.robots.check("https://a/docs/private/y", "ua", now=10.0, refresh=True,
                                       fetcher=lambda u: (200, ""))
        private = engine.check("https://a/docs/private/y", user_agent="ua", robots=private_ev)
        self.assertFalse(private.allowed)
        self.assertEqual(private.reason, "path_disallowed")
        outside = engine.check("https://a/other", user_agent="ua", robots=ok)
        self.assertFalse(outside.allowed)

    def test_evidence_for_another_path_is_rejected(self):
        ev = self.robots.check("https://a/docs/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, "User-agent: *\nAllow: /\n"))
        decision = self.engine.check("https://a/docs/other", user_agent="ua", robots=ev)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "robots_path_mismatch")

    def test_host_allowlist(self):
        engine = PolicyEngine(self.state)
        engine.activate({"default": "allow", "hosts": ["allowed.example"]})
        ev = self.robots.check("https://other.example/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, ""))
        self.assertFalse(engine.check("https://other.example/x", user_agent="ua", robots=ev).allowed)

    def test_depth_limit(self):
        engine = PolicyEngine(self.state)
        engine.activate({"default": "allow", "max_depth": 2})
        ev = self.robots.check("https://a/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, ""))
        self.assertTrue(engine.check("https://a/x", user_agent="ua", robots=ev, depth=2).allowed)
        self.assertFalse(engine.check("https://a/x", user_agent="ua", robots=ev, depth=3).allowed)

    def test_invalid_url_denied(self):
        decision = self.engine.check("mailto:x@y", user_agent="ua")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "invalid_url")

    def test_decision_explains_every_check(self):
        ev = self.robots.check("https://a/docs/x", "ua", now=10.0, refresh=True,
                               fetcher=lambda u: (200, ""))
        decision = self.engine.check("https://a/docs/x", user_agent="ua", robots=ev)
        names = [c.name for c in decision.checks]
        self.assertIn("path_rules", names)
        self.assertIn("robots", names)
        self.assertTrue(all("detail" in c.as_dict() for c in decision.checks))


class VersioningTests(unittest.TestCase):
    def test_activation_versions_and_previous(self):
        state, _ = make_state()
        engine = PolicyEngine(state)
        first, _ = engine.activate({"default": "allow"}, now=1.0)
        second, _ = engine.activate({"default": "deny"}, now=2.0)
        self.assertNotEqual(first.version, second.version)
        self.assertEqual(engine.active.version, second.version)
        versions = engine.versions()
        self.assertEqual(len(versions), 2)
        self.assertEqual(sum(v["active"] for v in versions), 1)

    def test_invalidate_skips_executed_and_unknown(self):
        state, _ = make_state()
        engine = PolicyEngine(state)
        engine.activate({"default": "allow", "allow": ["/*"]}, now=1.0)
        ev = RobotsStore(state, ttl=100).check(
            "https://a/x", "ua", now=1.0, refresh=True, fetcher=lambda u: (200, "")).as_dict()
        pending = [
            {"url": "https://a/x", "request_id": "pending", "robots": ev, "state": "pending"},
            {"url": "https://a/x", "request_id": "done", "robots": ev, "state": "executed"},
            {"url": "https://a/x", "request_id": "flying", "robots": ev, "state": "in_flight"},
            {"url": "https://a/x", "request_id": "lost", "robots": ev, "state": "unknown"},
        ]
        invalid = engine.invalidate(pending, rules=RuleSet.from_doc({"default": "deny"}))
        self.assertEqual([entry["request_id"] for entry in invalid], ["pending"])


if __name__ == "__main__":
    unittest.main()
