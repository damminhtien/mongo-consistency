from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_release_ready import (
    EXPECTED_RQ2_CELLS,
    RQ2_CONDITIONS,
    RQ2_EPISODE_PLAN,
    RQ2_SIGNATURE_CELLS,
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
    episodes: list[dict[str, object]] = []
    for episode_id, plan in RQ2_EPISODE_PLAN.items():
        episode_records: list[dict[str, object]] = []
        for property_name, configurations in EXPECTED_RQ2_CELLS.items():
            for configuration_id in sorted(configurations):
                signature_extension = (
                    plan["signature_extension"] is True
                    and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                )
                if plan["signature_extension"] is True and not signature_extension:
                    continue
                record = {
                    "trial_id": f"{episode_id}-{configuration_id}-{property_name}",
                    "topology_condition": plan["topology_condition"],
                    "fault_episode_id": episode_id,
                    "repetition": plan["repetition"],
                    "signature_extension": signature_extension,
                    "configuration_id": configuration_id,
                    "property": property_name,
                    "outcome": "PASS",
                    "runner_error": None,
                }
                episode_records.append(record)
                rq2_records.append(record)
        event: dict[str, object] = {
            "status": "APPLIED",
            "recovery_status": "CONVERGED",
            "coordinator_apply": {"ok": True, "action": "apply", "details": {}},
            "recovery_details": {"ok": True, "action": "recover"},
        }
        if plan["topology_condition"] == "F1":
            event["fault_verified"] = True
        if plan["topology_condition"] == "F3":
            event["coordinator_apply"] = {
                "ok": True,
                "action": "apply",
                "details": {
                    "controller": {"verified": True, "replication_isolated": True}
                },
            }
        episodes.append(
            {
                "episode_id": episode_id,
                "topology_condition": plan["topology_condition"],
                "repetition": plan["repetition"],
                "signature_extension": plan["signature_extension"],
                "history_count": len(episode_records),
                "trial_ids": [record["trial_id"] for record in episode_records],
                "fault_status": "APPLIED",
                "recovery_status": "CONVERGED",
                "error": None,
                "event": event,
            }
        )

    rq2_manifest = _campaign_manifest("rq2", rq2_records)
    rq2_manifest.update(
        {
            "fault_episode_count": len(RQ2_EPISODE_PLAN),
            "completed_fault_episode_count": len(episodes),
            "planned_episode_ids": list(RQ2_EPISODE_PLAN),
            "episodes": episodes,
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
            json.dumps(rq2_manifest if campaign == "rq2" else _campaign_manifest(campaign, records)),
            encoding="utf-8",
        )

    summary = {
        "status": "DATA",
        "campaign_summaries": {
            campaign: {"history_count": count}
            for campaign, count in (("pilot", 192), ("experiment", 1280), ("rq2", 432))
        },
        "fault_episode_summaries": [
            {
                "topology_condition": condition,
                "episode_count": episode_count,
            }
            for condition, episode_count in (("F1", 8), ("F2", 8), ("F3", 20))
        ],
        "groups": [
            {
                "campaign_id": "rq2",
                "topology_condition": condition,
                "configuration_id": configuration_id,
                "property": property_name,
                "normal_baseline": {
                    "campaign_id": "experiment",
                    "history_count": 10,
                },
            }
            for condition in RQ2_CONDITIONS
            for property_name, configurations in EXPECTED_RQ2_CELLS.items()
            for configuration_id in configurations
        ],
    }
    summary_path = root / "results/summary/summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    submission = root / "submission"
    submission.mkdir()
    (submission / "metadata.mk").write_text(
        "COURSE_CODE=DSA5208\n"
        "COURSE_TITLE=Scalable Distributed Computing for Data Science\n"
        "PROJECT_SUPERVISOR=Prof. Zhenning Cai\n"
        "PROJECT_TITLE=MongoDB consistency\n"
        "ACADEMIC_YEAR=AY2026/2027\n"
        "TEAM_NAME=Group\n"
        "TEAM_MEMBERS=DAM MINH TIEN (A0355091E); NGUYEN MINH DUC (A0000000X); "
        "VU NHAT MINH THU (A0000001X)\n"
        "STUDENT_EMAIL_MEMBER=DAM MINH TIEN\n"
        "STUDENT_EMAIL=student@example.edu\n"
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
