from __future__ import annotations

import json
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import scripts.analyse_rq3 as analyse_rq3
from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations
from mongo_consistency.history import write_history
from mongo_consistency.models import History
from mongo_consistency.rq3 import TOPOLOGY_PLANS, pair_control

from scripts.check_release_ready import (
    EXPECTED_RQ2_CELLS,
    RQ3_CONTRASTS,
    RQ2_CONDITIONS,
    RQ2_EPISODE_PLAN,
    RQ2_SIGNATURE_CELLS,
    _check_rq3,
    check_release_readiness,
)

RQ3_FIXTURE_PATH = ROOT / "tests/fixtures/rq3/protocol-v2-pairs.json"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_rq3_frozen_inputs(root: Path) -> None:
    for relative in (
        "configs/configurations.json",
        "configs/predictions.json",
        "configs/rq3-anchors.json",
        "docs/experimental-protocol.md",
        "scripts/run_rq3_campaign.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    shutil.copytree(ROOT / "src/mongo_consistency", root / "src/mongo_consistency")
    anchors = json.loads((root / "configs/rq3-anchors.json").read_text(encoding="utf-8"))
    for pair in anchors["pairs"]:
        for record in pair["histories"]:
            source = ROOT / record["path"]
            destination = root / record["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _rq3_valid_root(root: Path) -> None:
    """Build a compact, raw-backed v2 campaign that passes the rigor gate."""

    _copy_rq3_frozen_inputs(root)
    anchors = json.loads((root / "configs/rq3-anchors.json").read_text(encoding="utf-8"))
    configurations = load_configurations(root / "configs/configurations.json")
    fixture_pairs = json.loads(RQ3_FIXTURE_PATH.read_text(encoding="utf-8"))["pairs"]
    raw_root = root / "results/raw/rq3-v2"
    preflight_path = root / "results/raw/rq3-preflight.json"

    runtime_provenance: dict[str, object] = {
        "runner_commit": "a" * 40,
        "runner_dirty": False,
        "prediction_commit": "b" * 40,
        "prediction_manifest_hash": _sha256(root / "configs/predictions.json"),
        "protocol_commit": "c" * 40,
        "protocol_hash": _sha256(root / "docs/experimental-protocol.md"),
        "software_versions": {
            "python": "3.14.fixture",
            "pymongo": "fixture",
            "docker_engine": "fixture",
            "docker_compose": "fixture",
            "mongodb": "fixture",
        },
        "image_digest": ["sha256:" + "d" * 64],
        "checker_version": "history.v1",
    }
    preflight = {
        "schema_version": "rq3-preflight.v2",
        "protocol_id": "rq3-protocol.v2",
        "status": "PASS",
        "topology_plan": TOPOLOGY_PLANS["M3"].to_dict(),
        "planned_cycle_count": 1,
        "completed_cycle_count": 1,
        "passed_cycle_count": 1,
        "cycles": [
            {
                "cycle": cycle,
                "cycle_id": f"rq3-preflight-v2-c{cycle:02d}",
                "status": "PASS",
            }
            for cycle in range(1, 2)
        ],
        "runtime_provenance": runtime_provenance,
        "started_ns": 1,
        "last_updated_ns": 2,
        "finished_ns": 3,
    }
    preflight_path.parent.mkdir(parents=True, exist_ok=True)
    preflight_path.write_text(json.dumps(preflight, indent=2, sort_keys=True) + "\n")

    records: list[dict[str, object]] = []
    pair_controls: list[dict[str, object]] = []
    grouped_rows: dict[str, list[dict[str, object]]] = {key: [] for key in RQ3_CONTRASTS}
    records_by_pair: dict[str, list[dict[str, object]]] = {}
    ordinal_by_contrast = {key: 0 for key in RQ3_CONTRASTS}
    contrast_rows = []
    for contrast_id, spec in RQ3_CONTRASTS.items():
        pair_ids = [f"{contrast_id.lower()}-r{replicate:02d}" for replicate in range(1, 6)]
        contrast_rows.append(
            {
                "contrast_id": contrast_id,
                "left_configuration": spec["left_configuration"],
                "right_configuration": spec["right_configuration"],
                "property": spec["property"],
                "capture_operations": spec["capture_operations"],
                "pair_ids": pair_ids,
            }
        )
        for replicate, pair_id in enumerate(pair_ids, start=1):
            pair_seed = 700 + replicate
            arms = [spec["left_configuration"], spec["right_configuration"]]
            if replicate % 2 == 0:
                arms.reverse()
            pair_histories: dict[str, dict[str, object]] = {}
            pair_records: list[dict[str, object]] = []
            for configuration_id in arms:
                ordinal_by_contrast[contrast_id] += 1
                ordinal = ordinal_by_contrast[contrast_id]
                campaign = spec["campaign"]
                trial_id = (
                    f"{campaign}-{ordinal:05d}-{configuration_id.lower()}-"
                    f"{spec['property'].lower()}"
                )
                payload = json.loads(json.dumps(fixture_pairs[contrast_id]["histories"][configuration_id]))
                history_manifest = payload["manifest"]
                history_manifest.update(
                    {
                        "trial_id": trial_id,
                        "campaign_id": campaign,
                        "configuration_id": configuration_id,
                        "property": spec["property"],
                        "seed": pair_seed,
                        "rq3_contrast_id": contrast_id,
                        "rq3_pair_id": pair_id,
                        "rq3_pair_seed": pair_seed,
                        "rq3_replicate": replicate,
                        "schedule_id": f"fixture-schedule-{replicate:02d}",
                        "runner_commit": runtime_provenance["runner_commit"],
                        "runner_dirty": False,
                        "prediction_commit": runtime_provenance["prediction_commit"],
                        "prediction_manifest_hash": runtime_provenance["prediction_manifest_hash"],
                        "protocol_commit": runtime_provenance["protocol_commit"],
                        "protocol_hash": runtime_provenance["protocol_hash"],
                        "software_versions": runtime_provenance["software_versions"],
                        "image_digest": runtime_provenance["image_digest"],
                        "checker_version": runtime_provenance["checker_version"],
                        "rq3_protocol_id": "rq3-protocol.v2",
                        "rq3_topology_plan": TOPOLOGY_PLANS[contrast_id].to_dict(),
                        "topology_capture_operations": spec["capture_operations"],
                        "property_steps": {
                            operation_id: operation_id
                            for operation_id in spec["capture_operations"]
                        },
                    }
                )
                for operation in payload.get("operations", []):
                    if not isinstance(operation, dict):
                        continue
                    for stage in ("topology_before", "topology_after"):
                        snapshot = operation.get(stage)
                        if isinstance(snapshot, dict):
                            snapshot.setdefault("observed_at_ns", 100)
                operations = {
                    operation.get("operation_id"): operation
                    for operation in payload.get("operations", [])
                    if isinstance(operation, dict)
                }
                if contrast_id == "M2":
                    read = operations["read"]
                    write = operations["write"]
                    write["depends_on_read_id"] = "read"
                    write["depends_on_version"] = read.get("observed_version")
                    write["dependency_metadata"] = {
                        "depends_on_read_id": "read",
                        "depends_on_version": read.get("observed_version"),
                    }
                elif contrast_id == "M3":
                    second_write = operations["second_write"]
                    second_write["parent_write_id"] = "w1"
                    second_write["dependency_metadata"] = {"parent_write_id": "w1"}
                history = History.from_dict(payload)
                history_path = root / "results/raw/rq3-v2" / campaign / f"{trial_id}.json"
                history_hash = write_history(history_path, history)
                history_payload = history.to_dict()
                pair_histories[configuration_id] = history_payload
                record: dict[str, object] = {
                    "trial_id": trial_id,
                    "ordinal": ordinal,
                    "campaign": campaign,
                    "contrast_id": contrast_id,
                    "pair_id": pair_id,
                    "replicate": replicate,
                    "pair_seed": pair_seed,
                    "configuration_id": configuration_id,
                    "property": spec["property"],
                    "adversarial": True,
                    "seed": pair_seed,
                    "path": history_path.relative_to(root).as_posix(),
                    "history_hash": history_hash,
                    "outcome": check_history(history).outcome.value,
                    "precondition_status": history_payload["precondition"]["status"],
                    "runner_commit": runtime_provenance["runner_commit"],
                    "runner_error": None,
                }
                records.append(record)
                pair_records.append(record)
            pair_control_record = pair_control(
                contrast_id,
                pair_id,
                pair_histories,
                configurations=configurations,
            )
            pair_controls.append(pair_control_record)
            records_by_pair[pair_id] = pair_records
            for configuration_id, history_payload in pair_histories.items():
                record = next(
                    item for item in pair_records if item["configuration_id"] == configuration_id
                )
                history_payload["result"] = record["outcome"]
                history_payload["source_path"] = record["path"]
            grouped_rows[contrast_id].append(
                analyse_rq3._row_for_pair(
                    contrast_id,
                    pair_id,
                    pair_histories,
                    configurations,
                )
            )

    manifest_path = raw_root / "campaign-manifest.json"
    manifest: dict[str, object] = {
        "schema_version": "rq3-campaign.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign": "rq3",
        "status": "COMPLETE",
        "repetitions_per_contrast": 5,
        "seed_base": 700,
        "planned_case_count": 30,
        "completed_case_count": 30,
        "runner_commit": runtime_provenance["runner_commit"],
        "runner_script_sha256": _sha256(root / "scripts/run_rq3_campaign.py"),
        "configuration_sha256": _sha256(root / "configs/configurations.json"),
        "protocol_sha256": runtime_provenance["protocol_hash"],
        "topology_plan_sha256": _sha256(root / "src/mongo_consistency/rq3.py"),
        "preflight_sha256": _sha256(preflight_path),
        "anchor_manifest_sha256": _sha256(root / "configs/rq3-anchors.json"),
        "runtime_provenance": runtime_provenance,
        "contrasts": contrast_rows,
        "pair_controls": pair_controls,
        "records": records,
        "started_ns": 1,
        "last_updated_ns": 2,
        "finished_ns": 3,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    selected_pairs: dict[str, dict[str, object]] = {}
    contrast_summaries: dict[str, dict[str, object]] = {}
    for contrast_id, spec in RQ3_CONTRASTS.items():
        pair_id = f"{contrast_id.lower()}-r01"
        selected_histories = []
        for configuration_id in (spec["left_configuration"], spec["right_configuration"]):
            record = next(
                item
                for item in records_by_pair[pair_id]
                if item["configuration_id"] == configuration_id
            )
            selected_histories.append(
                {
                    "trial_id": record["trial_id"],
                    "path": record["path"],
                    "sha256": record["history_hash"],
                    "configuration_id": configuration_id,
                }
            )
        selected = {
            "pair_id": pair_id,
            "control_valid": True,
            "histories": selected_histories,
        }
        selected_pairs[contrast_id] = selected
        contrast_summaries[contrast_id] = {
            "planned_pair_count": 5,
            "control_valid_pair_count": 5,
            "invalid_pair_count": 0,
            "invalid_pairs": [],
            "signature_counts": analyse_rq3._counts(grouped_rows[contrast_id], contrast_id),
            "selected_pair": selected,
        }

    analysis_root = root / "results/analysis/rq3-v2"
    selection_path = analysis_root / "selection-manifest.json"
    analysis_root.mkdir(parents=True, exist_ok=True)
    selection = {
        "schema_version": "rq3-selection.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": manifest_path.relative_to(root).as_posix(),
        "campaign_manifest_sha256": _sha256(manifest_path),
        "preflight_sha256": _sha256(preflight_path),
        "anchor_manifest_sha256": _sha256(root / "configs/rq3-anchors.json"),
        "anchor_history_ids": [
            record["trial_id"]
            for pair in anchors["pairs"]
            for record in pair["histories"]
        ],
        "selected_pairs": selected_pairs,
    }
    selection_path.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
    submission = root / "submission"
    (submission / "sections").mkdir(parents=True, exist_ok=True)
    (submission / "figures").mkdir(parents=True, exist_ok=True)
    (submission / "sections/07-results.tex").write_text(
        "\\input{submission/generated-rq3.tex}\n", encoding="utf-8"
    )
    generated_tex = submission / "generated-rq3.tex"
    generated_tex.write_text(
        analyse_rq3._render_report_section(
            grouped_rows,
            selected_pairs,
            manifest,
            analyse_rq3._summarize_historical_anchors(anchors, repository_root=root),
        ),
        encoding="utf-8",
    )
    timeline_path = submission / "figures/rq3-causal-timeline.pdf"
    timeline_path.write_bytes(b"%PDF-1.4\n" + b"synthetic timeline fixture\n" * 32 + b"%%EOF\n")
    summary = {
        "schema_version": "rq3-analysis.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": manifest_path.relative_to(root).as_posix(),
        "campaign_manifest_sha256": _sha256(manifest_path),
        "preflight_sha256": _sha256(preflight_path),
        "anchor_manifest_sha256": _sha256(root / "configs/rq3-anchors.json"),
        "historical_anchors": analyse_rq3._summarize_historical_anchors(anchors, repository_root=root),
        "repetitions_per_contrast": 5,
        "contrast_summaries": contrast_summaries,
        "selection_manifest": selection_path.relative_to(root).as_posix(),
        "generated_artifacts": {
            "report_tex": {
                "path": "submission/generated-rq3.tex",
                "sha256": _sha256(generated_tex),
            },
            "timeline_pdf": {
                "path": "submission/figures/rq3-causal-timeline.pdf",
                "sha256": _sha256(timeline_path),
            },
        },
    }
    (analysis_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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
    _rq3_valid_root(root)


class ReleaseReadinessTests(unittest.TestCase):
    def test_complete_campaigns_and_metadata_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_root(root)
            self.assertEqual([], check_release_readiness(root))

    def test_rq3_rigor_gate_passes_without_student_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_root(root)
            (root / "submission/metadata.mk").unlink()
            errors: list[str] = []
            _check_rq3(root, errors)
        self.assertEqual([], errors)

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
