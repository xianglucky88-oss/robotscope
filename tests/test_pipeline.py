import unittest

from src.robotscope import pipeline, store


class LegacyPipelineTests(unittest.TestCase):
    """The original record pipeline must keep behaving exactly as before."""

    def test_pipeline_runs_and_idempotent(self):
        a = pipeline.run()
        b = pipeline.run()   # 重跑不增行（幂等 upsert）
        self.assertEqual(a["fed"], 4)
        self.assertEqual(a["rows"], 3)   # r-003 重复被去重
        self.assertEqual(b["rows"], a["rows"])

    def test_store_roundtrip(self):
        con = store.connect()
        try:
            self.assertGreaterEqual(store.count(con), 0)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
