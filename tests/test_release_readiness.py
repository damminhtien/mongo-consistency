from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_release_ready import (
    EXPECTED_RQ2_CELLS,
    RQ2_CONDITIONS,
    check_release_readiness,
)


def _campaign_manifest(campaign: str, records: list[dict[str, object]]) -> dict[str, object]:
    for ordinal, record in enumerate(records, start=1):
        record["ordinal"] = ordinal
    return {
        "schema_version": "campaign-run.v1",
        "campaign": campaign,
        "status": "COMPLETE",
        "expected_case_count": len(records),
        "completed_case_count": len(records),
        "case_count": len(records),
        "runner_dirty": False,
        "runner_commit": "a" * 40,
        "records": records,
    }


def _valid_root(root: Path) -> None:
    pilot_records: list[dict[str, object]] = []
    for configuration_number in range(1, 9):
        for property_name in EXPECTED_RQ2_CELLS:
            for adversarial, repetitions in ((False, 1), (True, 5)):
                for _ in range(repetitions):
                    pilot_records.append(
                        {
                            "configuration_id": f"C{configuration_number}",
                            "property": property_name,
                            "adversarial": adversarial,
                            "outcome": "PASS",
                            "runner_error": None,
                        }
                    )
    experiment_records: list[dict[str, object]] = []
    for configuration_number in range(1, 9):
        for property_name in EXPECTED_RQ2_CELLS:
            for adversarial, repetitions in ((False, 10), (True, 30)):
                for _ in range(repetitions):
                    experiment_records.append(
                        {
                            "configuration_id": f"C{configuration_number}",
                            "property": property_name,
                            "adversarial": adversarial,
                            "outcome": "PASS",
                            "runner_error": None,
                        }
                    )
    rq2_records: list[dict[str, object]] = []
    for condition in RQ2_CONDITIONS:
        for property_name, configurations in EXPECTED_RQ2_CELLS.items():
            for configuration_id in sorted(configurations):
                for _ in range(10):
                    rq2_records.append(
                        {
                            "topology_condition": condition,
                            "configuration_id": configuration_id,
                            "property": property_name,
                            "outcome": "PASS",
                            "runner_error": None,
                        }
                    )
    for campaign, records in (
        ("pilot", pilot_records),
        ("experiment", experiment_records),
        ("rq2", rq2_records),
    ):
        campaign_dir = root / "results/raw" / campaign
        campaign_dir.mkdir(parents=True)
        (campaign_dir / "campaign-manifest.json").write_text(
            json.dumps(_campaign_manifest(campaign, records)),
            encoding="utf-8",
        )

    summary = {
        "status": "DATA",
        "campaign_summaries": {
            campaign: {"history_count": count}
            for campaign, count in (("pilot", 192), ("experiment", 1280), ("rq2", 440))
        },
    }
    summary_path = root / "results/summary/summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    submission = root / "submission"
    submission.mkdir()
    (submission / "metadata.mk").write_text(
        "COURSE_CODE=DSA5208\n"
        "PROJECT_TITLE=MongoDB consistency\n"
        "TEAM_NAME=Group\n"
        "TEAM_MEMBERS=DAM MINH TIEN (A0355091E); NGUYEN MINH DUC (A0000000X); "
        "VU NHAT MINH THU (A0000001X)\n"
        "SUBMISSION_DATE=20 September 2026\n"
        "AI_USE_DISCLOSURE=Reviewed\n",
        encoding="utf-8",
    )


class ReleaseReadinessTests(unittest.TestCase):
    def test_complete_campaigns_and_metadata_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_root(root)
            self.assertEqual([], check_release_readiness(root))

    def test_incomplete_rq2_and_pending_student_id_block_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_root(root)
            (root / "results/raw/rq2/campaign-manifest.json").unlink()
            metadata = root / "submission/metadata.mk"
            metadata.write_text(
                metadata.read_text(encoding="utf-8").replace("A0000000X", "Student ID pending"),
                encoding="utf-8",
            )
            errors = check_release_readiness(root)
        self.assertTrue(any("rq2/campaign-manifest" in error for error in errors))
        self.assertTrue(any("missing student ID" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
