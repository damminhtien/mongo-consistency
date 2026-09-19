from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.build_submission import (
    METADATA_KEYS,
    REQUIRED_SECTIONS,
    REQUIRED_SUBMISSION_FILES,
    parse_metadata,
    write_generated_analysis,
)

ROOT = Path(__file__).resolve().parents[1]


class SubmissionLayoutTests(unittest.TestCase):
    def test_required_submission_sources_exist(self) -> None:
        paths = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
        missing = [str(path) for path in paths if not (ROOT / path).is_file()]
        self.assertEqual([], missing)

    def test_metadata_has_all_build_fields(self) -> None:
        values = parse_metadata(ROOT / "submission/metadata.mk")
        self.assertEqual(set(METADATA_KEYS), set(values))
        self.assertTrue(all(values[key] for key in METADATA_KEYS))

    def test_makefile_exposes_submission_target(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(".PHONY:", makefile)
        self.assertIn("check-docs", makefile)
        self.assertIn("setup", makefile)
        self.assertIn("submission:", makefile)
        self.assertIn("scripts/build_submission.py", makefile)

    def test_source_manifest_has_explicit_package_roots(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"configs"', source)
        self.assertIn('"submission"', source)
        self.assertIn('"output"', source)
        self.assertIn('"tmp"', source)

    def test_submission_package_includes_runtime_harness_sources(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        for path in ("compose.yaml", "requirements.txt", "pyproject.toml", "infra"):
            self.assertIn(path, source)

    def test_generated_analysis_macros_show_no_data_without_summary(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            target = Path(directory) / "generated-analysis.tex"
            write_generated_analysis(target, Path(directory))
            content = target.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\AnalysisStatus}{NO\_DATA}", content)
        self.assertIn(r"\newcommand{\LatencyMedian}{NO\_DATA}", content)

    def test_generated_report_uses_main_campaign_and_separates_conditions(self) -> None:
        from tempfile import TemporaryDirectory

        summary = {
            "status": "DATA",
            "history_count": 1472,
            "overall": {"consistency_violation_rate": 0.99},
            "campaign_summaries": {
                "pilot": {"history_count": 192},
                "experiment": {
                    "history_count": 1280,
                    "normal": {
                        "history_count": 320,
                        "outcomes": {"PASS": 264, "VIOLATION": 2},
                        "consistency_violation_rate": 2 / 266,
                        "operation_success_rate": 0.9,
                        "history_completion_rate": 0.99,
                        "latency_ms": {"p50": 1.0, "p95": 5.0, "p99": 10.0},
                        "election_ms": {"p50": None},
                        "recovery_ms": {"p50": None},
                    },
                    "adversarial": {
                        "history_count": 960,
                        "outcomes": {
                            "PASS": 434,
                            "VIOLATION": 6,
                            "UNSUPPORTED": 449,
                        },
                        "consistency_violation_rate": 6 / 440,
                        "operation_success_rate": 0.8,
                        "history_completion_rate": 0.5,
                        "latency_ms": {"p50": 2.0, "p95": 20.0, "p99": 100.0},
                        "election_ms": {"p50": 12000.0},
                        "recovery_ms": {"p50": 200.0},
                    },
                    "properties": {
                        "RYW": {
                            "history_count": 240,
                            "outcomes": {"PASS": 109, "VIOLATION": 6, "UNSUPPORTED": 111},
                        },
                        "MR": {
                            "history_count": 240,
                            "outcomes": {"PASS": 97},
                        },
                        "MW": {
                            "history_count": 240,
                            "outcomes": {"PASS": 104},
                        },
                        "WFR": {
                            "history_count": 240,
                            "outcomes": {"PASS": 124},
                        },
                    },
                },
            },
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "results/summary/summary.json"
            summary_path.parent.mkdir(parents=True)
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            manifest_path = root / "results/raw/experiment/campaign-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps({"status": "COMPLETE", "parallel_workers": 2}),
                encoding="utf-8",
            )
            target = root / "generated-analysis.tex"
            write_generated_analysis(target, root)
            content = target.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\HistoryCount}{1280}", content)
        self.assertIn(r"\newcommand{\PilotHistoryCount}{192}", content)
        self.assertIn(r"\newcommand{\NormalViolationCount}{2}", content)
        self.assertIn(r"\newcommand{\AdversarialUnsupportedCount}{449}", content)
        self.assertIn(r"\newcommand{\ConsistencyViolationRate}{0.0136}", content)
        self.assertIn(r"\newcommand{\MainCampaignStatus}{COMPLETE}", content)
        self.assertIn(r"\newcommand{\ParallelWorkerCount}{2}", content)
        self.assertIn(r"\newcommand{\RYWViolationCount}{6}", content)
        self.assertIn(r"\newcommand{\RYWDecidableCount}{115}", content)
        self.assertIn(r"\newcommand{\MRViolationCount}{0}", content)
        self.assertIn(r"\newcommand{\MRDecidableCount}{97}", content)
        self.assertNotIn(r"\newcommand{\ConsistencyViolationRate}{0.9900}", content)

    def test_schema_contracts_are_packaged(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"schemas"', source)
        self.assertTrue((ROOT / "schemas/history.v1.json").is_file())

    def test_parallel_worker_scratch_is_not_packaged(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('Path("results/parallel")', source)


if __name__ == "__main__":
    unittest.main()
