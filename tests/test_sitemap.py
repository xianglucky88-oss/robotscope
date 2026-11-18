import unittest

from tests.support import sitemapindex, urlset
from robotscope.sitemap import Discovery, SitemapWalker, deletion_candidates, parse_sitemap


def routes(mapping):
    def fetch(url):
        if url not in mapping:
            raise FileNotFoundError(f"missing {url}")
        value = mapping[url]
        if isinstance(value, BaseException):
            raise value
        return (200, value)
    return fetch


class ParseTests(unittest.TestCase):
    def test_urlset_merges_metadata(self):
        kind, entries, children = parse_sitemap(urlset("https://x/a", "https://x/b"))
        self.assertEqual(kind, "urlset")
        self.assertEqual([e.url for e in entries], ["https://x/a", "https://x/b"])
        self.assertEqual(children, [])

    def test_index_lists_children(self):
        kind, entries, children = parse_sitemap(
            sitemapindex("https://x/a.xml", "https://x/b.xml"))
        self.assertEqual(kind, "index")
        self.assertEqual(children, ["https://x/a.xml", "https://x/b.xml"])
        self.assertEqual(entries, [])

    def test_malformed_xml_degrades_to_text(self):
        kind, entries, _ = parse_sitemap("https://x/a\nhttps://x/b\n")
        self.assertEqual(kind, "text")
        self.assertEqual(len(entries), 2)

    def test_relative_locs_resolve_against_sitemap(self):
        _, entries, _ = parse_sitemap(urlset("/a/b"), source="https://x/sitemap.xml")
        self.assertEqual(entries[0].url, "https://x/a/b")


class TraversalTests(unittest.TestCase):
    def test_recursive_index_and_dedupe(self):
        docs = {
            "https://x/s.xml": sitemapindex("https://x/a.xml", "https://x/b.xml"),
            "https://x/a.xml": urlset("https://x/one", "https://x/shared"),
            "https://x/b.xml": urlset("https://x/two", "https://x/shared"),
        }
        d = SitemapWalker(None, fetcher=routes(docs)).walk(["https://x/s.xml"], now=1.0)
        self.assertTrue(d.complete)
        self.assertEqual(sorted(d.urls()), ["https://x/one", "https://x/shared", "https://x/two"])
        self.assertEqual(len(d.entries["https://x/shared"].sources), 2)

    def test_cycle_is_detected_not_looped(self):
        docs = {
            "https://x/s.xml": sitemapindex("https://x/a.xml"),
            "https://x/a.xml": sitemapindex("https://x/b.xml", "https://x/a.xml"),
            "https://x/b.xml": sitemapindex("https://x/a.xml"),
        }
        d = SitemapWalker(None, fetcher=routes(docs)).walk(["https://x/s.xml"], now=1.0)
        self.assertEqual(len(d.documents), 3)
        self.assertIn(("https://x/a.xml", "https://x/a.xml"), d.cycle_edges)
        self.assertIn(("https://x/b.xml", "https://x/a.xml"), d.cycle_edges)

    def test_shared_reference_recorded_separately_from_cycle(self):
        docs = {
            "https://x/s.xml": sitemapindex("https://x/a.xml", "https://x/b.xml"),
            "https://x/a.xml": sitemapindex("https://x/shared.xml"),
            "https://x/b.xml": sitemapindex("https://x/shared.xml"),
            "https://x/shared.xml": urlset("https://x/page"),
        }
        d = SitemapWalker(None, fetcher=routes(docs)).walk(["https://x/s.xml"], now=1.0)
        self.assertEqual(d.cycle_edges, [])
        self.assertEqual(len(d.shared_edges), 1)
        doc = d.documents["https://x/shared.xml"]
        self.assertEqual(sorted(doc.parents), ["https://x/a.xml", "https://x/b.xml"])
        self.assertIn("https://x/page", d.entries)

    def test_partial_failure_does_not_abort_scan(self):
        docs = {
            "https://x/s.xml": sitemapindex("https://x/a.xml", "https://x/b.xml", "https://x/c.xml"),
            "https://x/a.xml": urlset("https://x/a1"),
            "https://x/b.xml": RuntimeError("boom"),
            "https://x/c.xml": urlset("https://x/c1"),
        }
        d = SitemapWalker(None, fetcher=routes(docs)).walk(["https://x/s.xml"], now=5.0)
        self.assertFalse(d.complete)
        self.assertEqual(sorted(d.urls()), ["https://x/a1", "https://x/c1"])
        self.assertEqual([f["url"] for f in d.failures], ["https://x/b.xml"])
        self.assertEqual(d.failures[0]["error"].split(":")[0], "RuntimeError")

    def test_incomplete_scan_blocks_deletion_inference(self):
        docs = {
            "https://x/s.xml": sitemapindex("https://x/a.xml", "https://x/b.xml"),
            "https://x/a.xml": urlset("https://x/a1"),
            "https://x/b.xml": RuntimeError("boom"),
        }
        d = SitemapWalker(None, fetcher=routes(docs)).walk(["https://x/s.xml"], now=5.0)
        self.assertEqual(deletion_candidates(d, ["https://x/a1", "https://x/gone"]), [])

    def test_complete_scan_reports_deletions(self):
        d = SitemapWalker(None, fetcher=routes(
            {"https://x/s.xml": urlset("https://x/a1")})).walk(["https://x/s.xml"], now=5.0)
        self.assertTrue(d.complete)
        self.assertEqual(deletion_candidates(d, ["https://x/a1", "https://x/gone"]),
                         ["https://x/gone"])

    def test_cache_prevents_refetch_and_keeps_versions(self):
        from tests.support import make_state
        state, _ = make_state()
        calls = []

        def fetch(url):
            calls.append(url)
            return (200, urlset("https://x/a1"))

        walker = SitemapWalker(state, fetcher=fetch, ttl=100)
        first = walker.walk(["https://x/s.xml"], now=10.0)
        self.assertEqual(len(calls), 1)
        second = walker.walk(["https://x/s.xml"], now=20.0)
        self.assertEqual(len(calls), 1)  # cache hit
        self.assertEqual(second.documents["https://x/s.xml"].status, "cached")
        self.assertEqual(first.documents["https://x/s.xml"].version,
                         second.documents["https://x/s.xml"].version)

    def test_max_depth_marks_incomplete(self):
        docs = {
            "https://x/0.xml": sitemapindex("https://x/1.xml"),
            "https://x/1.xml": sitemapindex("https://x/2.xml"),
            "https://x/2.xml": urlset("https://x/a"),
        }
        d = SitemapWalker(None, fetcher=routes(docs), max_depth=1).walk(["https://x/0.xml"], now=1.0)
        self.assertFalse(d.complete)
        self.assertEqual(deletion_candidates(d, ["https://x/gone"]), [])


if __name__ == "__main__":
    unittest.main()
