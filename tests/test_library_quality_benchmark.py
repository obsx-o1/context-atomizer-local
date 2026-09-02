from __future__ import annotations

import unittest
from pathlib import Path

from test_support import TemporaryDatabaseTest

from atomizer_local_client.evaluation.quality_benchmark import run_quality_benchmark


class LibraryQualityBenchmarkTests(TemporaryDatabaseTest):
    def test_disposable_quality_fixture_is_deterministic_and_inspectable(self) -> None:
        result = run_quality_benchmark(self.root / "quality-fixture")
        self.assertEqual(result["schema_version"], "context-atomizer-library-quality-v1")
        self.assertFalse(result["fixture"]["model_as_judge"])
        self.assertFalse(result["fixture"]["retrieval_algorithms_changed"])
        metrics = result["metrics"]
        self.assertGreaterEqual(metrics["recall_at_8"], 0.8)
        self.assertEqual(metrics["project_scope_bleed_rate"], 0.0)
        self.assertLessEqual(metrics["invalid_stale_evidence_rate"], 0.25)
        self.assertEqual(metrics["provenance_coverage"], 1.0)
        self.assertTrue(metrics["recent_order_is_descending"])
        self.assertGreater(metrics["result_count"], 0)
        self.assertGreater(metrics["output_characters"], 0)


if __name__ == "__main__":
    unittest.main()
