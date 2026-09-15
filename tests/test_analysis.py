from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.analysis import analyse
from mongo_consistency.models import History, OperationRecord
from mongo_consistency.history import write_history


class AnalysisTests(unittest.TestCase):
    def test_empty_analysis_is_explicitly_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = analyse(
                raw_root=root / "raw",
                summary_root=root / "summary",
                figures_root=root / "figures",
            )
            self.assertEqual("NO_DATA", summary["status"])
            self.assertEqual(0, summary["history_count"])
            self.assertTrue((root / "summary/summary.json").is_file())
            self.assertTrue((root / "figures/outcome-heatmap.svg").is_file())

    def test_analysis_rebuilds_counts_from_raw_history(self) -> None:
        history = History(
            manifest={
                "trial_id": "normal-00001-C1-ryw",
                "campaign_id": "normal",
                "configuration_id": "C1",
                "property": "RYW",
                "adversarial": False,
            },
            operations=[
                OperationRecord(
                    operation_id="write",
                    kind="write",
                    key="x",
                    version=1,
                    write_id="w1",
                    start_ns=0,
                    end_ns=1_000_000,
                ),
                OperationRecord(
                    operation_id="read",
                    kind="read",
                    key="x",
                    observed_version=1,
                    start_ns=1_000_000,
                    end_ns=3_000_000,
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_history(root / "raw/normal/trial.json", history)
            summary = analyse(
                raw_root=root / "raw",
                summary_root=root / "summary",
                figures_root=root / "figures",
            )
            self.assertEqual("DATA", summary["status"])
            self.assertEqual(1, summary["history_count"])
            self.assertEqual(1, summary["outcome_counts"]["PASS"])
            self.assertEqual(1.0, summary["groups"][0]["operation_success_rate"])
            self.assertEqual(1.5, summary["groups"][0]["latency_ms"]["p50"])
            parsed = json.loads((root / "summary/summary.json").read_text())
            self.assertEqual("DATA", parsed["status"])


if __name__ == "__main__":
    unittest.main()
