"""Reject tagged releases with incomplete evidence or student metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__:
    from .build_submission import BuildError, parse_metadata
else:
    from build_submission import BuildError, parse_metadata

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CAMPAIGNS = {"pilot": 192, "experiment": 256, "rq2": 432}
RQ2_CONFIGURATIONS = {"C1", "C3", "C4", "C6"}
EXPECTED_RQ2_CELLS = {property_name: RQ2_CONFIGURATIONS for property_name in ("RYW", "MR", "MW", "WFR")}
RQ2_CONDITIONS = ("F1", "F2", "F3")
RQ2_SIGNATURE_CELLS = {("C1", "RYW"), ("C6", "RYW"), ("C1", "MW"), ("C6", "MW")}
RQ2_EPISODE_IDS = {
    f"rq2-{condition.lower()}-r{repetition:02d}"
    for repetition in range(1, 9)
    for condition in RQ2_CONDITIONS
} | {f"rq2-f3-r{repetition:02d}" for repetition in range(9, 21)}
RQ2_EPISODE_COUNTS = {"F1": 8, "F2": 8, "F3": 20}
RQ2_EPISODE_PLAN = {
    f"rq2-{condition.lower()}-r{repetition:02d}": {
        "topology_condition": condition,
        "repetition": repetition,
        "signature_extension": False,
        "history_count": 16,
    }
    for repetition in range(1, 9)
    for condition in RQ2_CONDITIONS
} | {
    f"rq2-f3-r{repetition:02d}": {
        "topology_condition": "F3",
        "repetition": repetition,
        "signature_extension": True,
        "history_count": 4,
    }
    for repetition in range(9, 21)
}
EXPECTED_SUMMARY_COUNTS = {"pilot": 192, "experiment": 256, "rq2": 432}
RQ3_CONTRASTS = {
    "M1": {
        "campaign": "rq3-m1",
        "configurations": {"C5", "C6"},
        "left_configuration": "C5",
        "right_configuration": "C6",
        "property": "RYW",
        "capture_operations": ["write", "read"],
    },
    "M2": {
        "campaign": "rq3-m2",
        "configurations": {"C8", "C5"},
        "left_configuration": "C8",
        "right_configuration": "C5",
        "property": "WFR",
        "capture_operations": ["read", "write"],
    },
    "M3": {
        "campaign": "rq3-m3",
        "configurations": {"C3", "C6"},
        "left_configuration": "C3",
        "right_configuration": "C6",
        "property": "MW",
        "capture_operations": ["first_write", "second_write"],
    },
}
RQ3_REPETITIONS = 8
RQ3_EXPECTED_ROUTES = {
    "M1": {"write": "mongo3", "read": "mongo1"},
    "M2": {"read": "mongo3", "write": "mongo2"},
    "M3": {"first_write": "mongo3", "second_write": "mongo2"},
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a JSON object")
        return None
    return value


def _check_campaign(root: Path, campaign: str, expected_count: int, errors: list[str]) -> None:
    path = root / "results/raw" / campaign / "campaign-manifest.json"
    manifest = _read_object(path, errors)
    if manifest is None:
        return
    if manifest.get("status") != "COMPLETE":
        errors.append(f"{campaign}: status must be COMPLETE")
    for field in ("expected_case_count", "completed_case_count", "case_count"):
        if manifest.get(field) != expected_count:
            errors.append(f"{campaign}: {field} must be {expected_count}")
    if manifest.get("runner_dirty") is not False:
        errors.append(f"{campaign}: runner provenance is missing or dirty")
    if not manifest.get("runner_commit"):
        errors.append(f"{campaign}: runner commit is missing")

    records = manifest.get("records")
    if not isinstance(records, list):
        errors.append(f"{campaign}: records must be a list")
        return
    if len(records) != expected_count:
        errors.append(f"{campaign}: expected {expected_count} records, found {len(records)}")
        return
    record_objects = [record for record in records if isinstance(record, dict)]
    if len(record_objects) != len(records):
        errors.append(f"{campaign}: records contain a non-object entry")
    ordinals = [record.get("ordinal") for record in record_objects]
    valid_ordinals = [value for value in ordinals if type(value) is int]
    if len(valid_ordinals) != expected_count or set(valid_ordinals) != set(range(1, expected_count + 1)):
        errors.append(f"{campaign}: record ordinals are incomplete or duplicated")
    if any(
        not isinstance(record, dict)
        or record.get("outcome") == "HARNESS_ERROR"
        or record.get("runner_error") is not None
        for record in records
    ):
        errors.append(f"{campaign}: contains a harness or runner error")

    if campaign in {"pilot", "experiment"}:
        counts = Counter(
            (
                record.get("configuration_id"),
                record.get("property"),
                bool(record.get("adversarial")),
            )
            for record in records
            if isinstance(record, dict)
        )
        for configuration_number in range(1, 9):
            configuration_id = f"C{configuration_number}"
            for property_name in EXPECTED_RQ2_CELLS:
                repetitions_by_mode = (
                    ((False, 1), (True, 5))
                    if campaign == "pilot"
                    else ((False, 3), (True, 5))
                )
                for adversarial, repetitions in repetitions_by_mode:
                    if counts[(configuration_id, property_name, adversarial)] != repetitions:
                        errors.append(
                            f"{campaign}: {configuration_id}/{property_name}/"
                            f"adversarial={adversarial} must have {repetitions} records"
                        )
    else:
        records_by_episode: dict[str, list[dict[str, Any]]] = {}
        counts = Counter(
            (
                record.get("topology_condition"),
                record.get("configuration_id"),
                record.get("property"),
            )
            for record in records
            if isinstance(record, dict)
        )
        expected_cells = {
            (condition, configuration_id, property_name)
            for condition in RQ2_CONDITIONS
            for property_name, configurations in EXPECTED_RQ2_CELLS.items()
            for configuration_id in configurations
        }
        if set(counts) != expected_cells:
            errors.append("rq2: topology-condition/configuration/property cells are incomplete or unexpected")
        for condition, configuration_id, property_name in sorted(expected_cells):
            expected_repetitions = (
                20
                if condition == "F3" and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                else 8
            )
            if counts[(condition, configuration_id, property_name)] != expected_repetitions:
                errors.append(
                    f"rq2: {condition}/{configuration_id}/{property_name} must have "
                    f"{expected_repetitions} records"
                )
        repetitions = Counter(
            (
                record.get("topology_condition"),
                record.get("configuration_id"),
                record.get("property"),
                record.get("repetition"),
            )
            for record in records
            if isinstance(record, dict)
        )
        expected_repetitions = {
            (condition, configuration_id, property_name, repetition)
            for condition in RQ2_CONDITIONS
            for configuration_id in RQ2_CONFIGURATIONS
            for property_name in EXPECTED_RQ2_CELLS
            for repetition in range(
                1,
                21 if condition == "F3" and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS else 9,
            )
        }
        if set(repetitions) != expected_repetitions or any(count != 1 for count in repetitions.values()):
            errors.append("rq2: repetition indices are incomplete, duplicated, or outside the registered plan")
        for record in record_objects:
            condition = record.get("topology_condition")
            repetition = record.get("repetition")
            expected_episode_id = (
                f"rq2-{condition.lower()}-r{repetition:02d}"
                if isinstance(condition, str)
                and isinstance(repetition, int)
                and not isinstance(repetition, bool)
                else None
            )
            if record.get("fault_episode_id") != expected_episode_id:
                errors.append(f"rq2: history {record.get('trial_id')} has a mismatched fault_episode_id")
            expected_plan = RQ2_EPISODE_PLAN.get(expected_episode_id)
            configuration_id = record.get("configuration_id")
            property_name = record.get("property")
            if expected_plan is not None:
                is_signature_extension = (
                    expected_plan["signature_extension"] is True
                    and isinstance(configuration_id, str)
                    and isinstance(property_name, str)
                    and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                )
                if record.get("signature_extension") is not is_signature_extension:
                    errors.append(f"rq2: history {record.get('trial_id')} has a mismatched signature flag")
                trial_id = record.get("trial_id")
                if isinstance(trial_id, str):
                    records_by_episode.setdefault(expected_episode_id, []).append(record)

        episodes = manifest.get("episodes")
        episode_entries = episodes if isinstance(episodes, list) else []
        episode_ids = [
            episode.get("episode_id")
            for episode in episode_entries
            if isinstance(episode, dict) and isinstance(episode.get("episode_id"), str)
        ]
        planned_episode_ids = manifest.get("planned_episode_ids")
        episode_plan_valid = (
            manifest.get("fault_episode_count") == len(RQ2_EPISODE_IDS)
            and manifest.get("completed_fault_episode_count") == len(RQ2_EPISODE_IDS)
            and isinstance(planned_episode_ids, list)
            and len(planned_episode_ids) == len(RQ2_EPISODE_IDS)
            and all(isinstance(episode_id, str) for episode_id in planned_episode_ids)
            and set(planned_episode_ids) == RQ2_EPISODE_IDS
            and len(episode_entries) == len(RQ2_EPISODE_IDS)
            and set(episode_ids) == RQ2_EPISODE_IDS
            and len(episode_ids) == len(set(episode_ids))
        )
        if not episode_plan_valid:
            errors.append("rq2: campaign manifest must contain all 36 registered fault episodes")
        for episode in episode_entries:
            if not isinstance(episode, dict):
                continue
            episode_id = episode.get("episode_id")
            expected_plan = RQ2_EPISODE_PLAN.get(episode_id) if isinstance(episode_id, str) else None
            if expected_plan is None:
                continue
            event = episode.get("event")
            if (
                episode.get("fault_status") != "APPLIED"
                or episode.get("recovery_status") != "CONVERGED"
                or episode.get("error") is not None
                or not isinstance(event, dict)
                or event.get("status") != "APPLIED"
                or event.get("recovery_status") != "CONVERGED"
            ):
                errors.append(f"rq2: {episode_id} lacks verified fault application or recovery")
            if expected_plan["topology_condition"] == "F1" and (
                not isinstance(event, dict) or event.get("fault_verified") is not True
            ):
                errors.append(f"rq2: {episode_id} lacks surviving-topology verification")
            if expected_plan["topology_condition"] == "F3":
                apply = event.get("coordinator_apply") if isinstance(event, dict) else None
                details = apply.get("details") if isinstance(apply, dict) else None
                controller = details.get("controller") if isinstance(details, dict) else None
                if (
                    not isinstance(controller, dict)
                    or controller.get("verified") is not True
                    or controller.get("replication_isolated") is not True
                ):
                    errors.append(f"rq2: {episode_id} lacks verified partition evidence")
            expected_history_count = expected_plan["history_count"]
            if (
                episode.get("history_count") != expected_history_count
                or episode.get("topology_condition") != expected_plan["topology_condition"]
                or episode.get("repetition") != expected_plan["repetition"]
                or episode.get("signature_extension") is not expected_plan["signature_extension"]
            ):
                errors.append(
                    f"rq2: {episode_id} metadata or history count differs from the registered plan"
                )
            episode_records = records_by_episode.get(episode_id, [])
            record_ids = [record.get("trial_id") for record in episode_records]
            listed_ids = episode.get("trial_ids")
            if (
                len(episode_records) != expected_history_count
                or not isinstance(listed_ids, list)
                or not all(isinstance(trial_id, str) for trial_id in listed_ids)
                or len(listed_ids) != len(set(listed_ids))
                or not all(isinstance(trial_id, str) for trial_id in record_ids)
                or set(listed_ids) != set(record_ids)
            ):
                errors.append(f"rq2: {episode_id} trial_ids do not match its history records")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _check_topology_snapshot(
    snapshot: Any,
    *,
    history_path: Path,
    operation_id: str,
    stage: str,
    errors: list[str],
) -> None:
    prefix = f"rq3: {history_path} operation {operation_id} topology_{stage}"
    if not isinstance(snapshot, dict):
        errors.append(f"{prefix} snapshot is missing")
        return
    if type(snapshot.get("observed_at_ns")) is not int or snapshot["observed_at_ns"] < 0:
        errors.append(f"{prefix} observed_at_ns is missing or invalid")
    term = snapshot.get("term")
    if "term" not in snapshot or (term is not None and (type(term) is not int or term < 0)):
        errors.append(f"{prefix} election term is missing or invalid")
    members = snapshot.get("members")
    if not isinstance(members, dict) or not {"mongo1", "mongo2", "mongo3"}.issubset(members):
        errors.append(f"{prefix} must contain direct observations for mongo1, mongo2, and mongo3")
        return
    for member in ("mongo1", "mongo2", "mongo3"):
        state = members.get(member)
        if not isinstance(state, dict) or not isinstance(state.get("reachable"), bool):
            errors.append(f"{prefix} has an invalid direct observation for {member}")


def _check_rq3_operation_captures(
    history: dict[str, Any],
    *,
    history_path: Path,
    contrast_id: str,
    capture_operations: Any,
    errors: list[str],
) -> None:
    if (
        not isinstance(capture_operations, list)
        or not capture_operations
        or any(not isinstance(operation_id, str) for operation_id in capture_operations)
    ):
        errors.append(f"rq3: {contrast_id} has no topology capture operation plan")
        return
    manifest = history.get("manifest")
    if not isinstance(manifest, dict):
        errors.append(f"rq3: history {history_path} manifest is invalid")
        return
    if manifest.get("topology_capture_operations") != capture_operations:
        errors.append(f"rq3: history {history_path} topology capture plan differs from its contrast")
    property_steps = manifest.get("property_steps")
    step_values = list(property_steps.values()) if isinstance(property_steps, dict) else []
    if (
        not isinstance(property_steps, dict)
        or any(not isinstance(value, str) for value in step_values)
        or any(not isinstance(value, str) for value in capture_operations)
        or set(step_values) != set(capture_operations)
    ):
        errors.append(f"rq3: history {history_path} property steps differ from its capture plan")

    first_operations = {"M1": "write", "M2": "read", "M3": "first_write"}
    followup_operations = {"M1": "read", "M2": "write", "M3": "second_write"}
    raw_operations = history.get("operations")
    if not isinstance(raw_operations, list):
        errors.append(f"rq3: history {history_path} operations are invalid")
        raw_operations = []
    operations = {
        operation.get("operation_id"): operation
        for operation in raw_operations
        if isinstance(operation, dict) and isinstance(operation.get("operation_id"), str)
    }
    precondition = history.get("precondition", {})
    precondition_status = precondition.get("status") if isinstance(precondition, dict) else None
    schedule_outcome = manifest.get("schedule_outcome")
    for operation_id in capture_operations:
        operation = operations.get(operation_id)
        if operation is None:
            allowed = precondition_status == "PRECONDITION_MISS"
            if operation_id == followup_operations.get(contrast_id):
                preceding = operations.get(first_operations.get(contrast_id))
                allowed = allowed or (
                    preceding is not None
                    and preceding.get("operation_status")
                    in {"UNAVAILABLE", "INDETERMINATE", "HARNESS_ERROR"}
                )
                allowed = allowed or (
                    isinstance(schedule_outcome, dict)
                    and schedule_outcome.get("outcome") in {"UNAVAILABLE", "INDETERMINATE"}
                )
            if not allowed:
                errors.append(f"rq3: history {history_path} is missing reached operation {operation_id}")
            continue
        for stage in ("before", "after"):
            _check_topology_snapshot(
                operation.get(f"topology_{stage}"),
                history_path=history_path,
                operation_id=str(operation_id),
                stage=stage,
                errors=errors,
            )


def _check_rq3_preflight(
    root: Path,
    *,
    runtime_provenance: dict[str, Any],
    topology_plan: dict[str, Any],
    expected_digest: Any,
    errors: list[str],
) -> str | None:
    path = root / "results/raw/rq3-preflight.json"
    preflight = _read_object(path, errors)
    if preflight is None:
        return None
    if (
        preflight.get("schema_version") != "rq3-preflight.v2"
        or preflight.get("protocol_id") != "rq3-protocol.v2"
        or preflight.get("status") != "PASS"
        or preflight.get("topology_plan") != topology_plan
        or preflight.get("planned_cycle_count") != 10
        or preflight.get("completed_cycle_count") != 10
        or preflight.get("passed_cycle_count") != 10
    ):
        errors.append("rq3: topology rehearsal must pass all ten cycles for the registered M3 plan")
    cycles = preflight.get("cycles")
    if not isinstance(cycles, list) or len(cycles) != 10:
        errors.append("rq3: topology rehearsal must contain exactly ten cycle records")
    else:
        for expected_cycle, cycle in enumerate(cycles, start=1):
            if (
                not isinstance(cycle, dict)
                or type(cycle.get("cycle")) is not int
                or cycle.get("cycle") != expected_cycle
                or cycle.get("cycle_id") != f"rq3-preflight-v2-c{expected_cycle:02d}"
                or cycle.get("status") != "PASS"
            ):
                errors.append(f"rq3: topology rehearsal cycle {expected_cycle} did not pass as planned")
    if preflight.get("runtime_provenance") != runtime_provenance:
        errors.append("rq3: topology preflight runtime provenance differs from the RQ3 campaign")
    digest = _sha256(path)
    if not _is_sha256(expected_digest) or digest != expected_digest:
        errors.append("rq3: topology preflight digest differs from the campaign manifest")
    return digest


def _operation(history: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    return next(
        (
            operation
            for operation in history.get("operations", [])
            if isinstance(operation, dict) and operation.get("operation_id") == operation_id
        ),
        None,
    )


def _check_rq3(root: Path, errors: list[str]) -> None:
    """Require complete protocol-v2 RQ3 controls and raw-derived analysis."""

    raw_root = root / "results/raw/rq3"
    manifest_path = raw_root / "campaign-manifest.json"
    manifest = _read_object(manifest_path, errors)
    if manifest is None:
        return
    if (
        manifest.get("schema_version") != "rq3-campaign.v2"
        or manifest.get("protocol_id") != "rq3-protocol.v2"
        or manifest.get("campaign") != "rq3"
    ):
        errors.append("rq3: campaign manifest schema or protocol identifier is invalid")
    if manifest.get("status") != "COMPLETE":
        errors.append("rq3: campaign status must be COMPLETE")
    repetitions = manifest.get("repetitions_per_contrast")
    if type(repetitions) is not int or repetitions != RQ3_REPETITIONS:
        errors.append(f"rq3: repetitions_per_contrast must be {RQ3_REPETITIONS}")
    expected_count = RQ3_REPETITIONS * 6
    for field in ("planned_case_count", "completed_case_count"):
        if manifest.get(field) != expected_count:
            errors.append(f"rq3: {field} must be {expected_count}")

    provenance = manifest.get("runtime_provenance")
    if not isinstance(provenance, dict):
        errors.append("rq3: runtime_provenance is missing")
        provenance = {}
    if provenance.get("runner_dirty") is not False:
        errors.append("rq3: runner provenance must be clean")
    if provenance.get("runner_commit") != manifest.get("runner_commit"):
        errors.append("rq3: runtime and campaign runner commits differ")
    for field in ("runner_commit", "protocol_commit", "prediction_commit"):
        value = manifest.get(field) if field == "runner_commit" else provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"rq3: {field} is missing")
    for field in ("protocol_hash", "prediction_manifest_hash"):
        if not _is_sha256(provenance.get(field)):
            errors.append(f"rq3: {field} must be a SHA-256 digest")
    prediction_digest = _sha256(root / "configs/predictions.json")
    if prediction_digest is None or provenance.get("prediction_manifest_hash") != prediction_digest:
        errors.append("rq3: prediction manifest hash differs from configs/predictions.json")
    software_versions = provenance.get("software_versions")
    required_versions = ("python", "pymongo", "docker_engine", "docker_compose", "mongodb")
    if not isinstance(software_versions, dict) or any(
        not isinstance(software_versions.get(key), str)
        or not software_versions[key].strip()
        for key in required_versions
    ):
        errors.append("rq3: runtime software version provenance is incomplete")
    image_digests = provenance.get("image_digest")
    if (
        not isinstance(image_digests, list)
        or not image_digests
        or any(not isinstance(value, str) or not value.strip() for value in image_digests)
    ):
        errors.append("rq3: MongoDB image digest provenance is incomplete")
    if provenance.get("checker_version") != "history.v1":
        errors.append("rq3: checker version must be history.v1")
    for field in (
        "runner_script_sha256",
        "configuration_sha256",
        "protocol_sha256",
        "topology_plan_sha256",
        "preflight_sha256",
        "anchor_manifest_sha256",
    ):
        if not _is_sha256(manifest.get(field)):
            errors.append(f"rq3: {field} must be a SHA-256 digest")
    expected_frozen_files = {
        "runner_script_sha256": root / "scripts/run_rq3_campaign.py",
        "configuration_sha256": root / "configs/configurations.json",
        "protocol_sha256": root / "docs/experimental-protocol.md",
        "topology_plan_sha256": root / "src/mongo_consistency/rq3.py",
        "anchor_manifest_sha256": root / "configs/rq3-anchors.json",
    }
    for field, path in expected_frozen_files.items():
        actual = _sha256(path)
        if actual is None or manifest.get(field) != actual:
            errors.append(f"rq3: {field} does not match {path.relative_to(root).as_posix()}")
    if provenance.get("protocol_hash") != manifest.get("protocol_sha256"):
        errors.append("rq3: protocol hash differs from the frozen protocol digest")

    source_root = root / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    repository_root = str(root)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        from mongo_consistency.config import load_configurations
        from mongo_consistency.checkers import check_history
        from mongo_consistency.history import read_history
        from mongo_consistency.rq3 import (
            EXPECTED_ROUTES,
            TOPOLOGY_PLANS,
            pair_control,
        )
        from mongo_consistency.rq3_anchors import verify_anchor_manifest
        from scripts.analyse_rq3 import (
            _counts,
            _row_for_pair,
            _summarize_historical_anchors,
        )
    except ImportError as error:
        errors.append(f"rq3: cannot load protocol-v2 validators: {error}")
        return
    if EXPECTED_ROUTES != RQ3_EXPECTED_ROUTES:
        errors.append("rq3: release guard routes differ from the protocol-v2 topology plan")
    try:
        configurations = load_configurations(root / "configs/configurations.json")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        errors.append(f"rq3: cannot load configuration settings: {error}")
        return

    try:
        anchor_manifest, anchor_digest = verify_anchor_manifest(root)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as error:
        errors.append(f"rq3: cannot verify selected RQ1 anchors: {error}")
        anchor_manifest = {}
        anchor_digest = None
    if manifest.get("anchor_manifest_sha256") != anchor_digest:
        errors.append("rq3: campaign anchor digest differs from the selected RQ1 histories")

    m3_plan = TOPOLOGY_PLANS["M3"].to_dict()
    _check_rq3_preflight(
        root,
        runtime_provenance=provenance,
        topology_plan=m3_plan,
        expected_digest=manifest.get("preflight_sha256"),
        errors=errors,
    )

    registered_contrasts = manifest.get("contrasts")
    contrast_rows = {
        row.get("contrast_id"): row
        for row in registered_contrasts
        if isinstance(row, dict) and isinstance(row.get("contrast_id"), str)
    } if isinstance(registered_contrasts, list) else {}
    contrast_count = len(registered_contrasts) if isinstance(registered_contrasts, list) else 0
    if set(contrast_rows) != set(RQ3_CONTRASTS) or contrast_count != len(RQ3_CONTRASTS):
        errors.append("rq3: campaign must register exactly M1, M2, and M3")
    expected_pair_ids_by_contrast: dict[str, list[str]] = {}
    for contrast_id, spec in RQ3_CONTRASTS.items():
        row = contrast_rows.get(contrast_id, {})
        if (
            row.get("left_configuration") != spec["left_configuration"]
            or row.get("right_configuration") != spec["right_configuration"]
            or row.get("property") != spec["property"]
        ):
            errors.append(f"rq3: {contrast_id} has an invalid configuration pair or property")
        if row.get("capture_operations") != spec["capture_operations"]:
            errors.append(f"rq3: {contrast_id} topology capture plan differs from the registered operations")
        pair_ids = [f"{contrast_id.lower()}-r{rep:02d}" for rep in range(1, RQ3_REPETITIONS + 1)]
        expected_pair_ids_by_contrast[contrast_id] = pair_ids
        if row.get("pair_ids") != pair_ids:
            errors.append(f"rq3: {contrast_id} pair_ids do not match the registered repetitions")

    records = manifest.get("records")
    if not isinstance(records, list) or len(records) != expected_count:
        errors.append(f"rq3: expected {expected_count} history records")
        return
    record_by_trial: dict[str, dict[str, Any]] = {}
    records_by_pair: dict[str, list[dict[str, Any]]] = {}
    histories_by_pair: dict[str, dict[str, dict[str, Any]]] = {}
    paths: set[str] = set()
    ordinals: set[tuple[str, int]] = set()

    for record in records:
        if not isinstance(record, dict):
            errors.append("rq3: records contain a non-object entry")
            continue
        trial_id = record.get("trial_id")
        contrast_id = record.get("contrast_id")
        pair_id = record.get("pair_id")
        spec = RQ3_CONTRASTS.get(contrast_id) if isinstance(contrast_id, str) else None
        if not isinstance(trial_id, str) or not trial_id or trial_id in record_by_trial:
            errors.append("rq3: trial IDs must be nonempty and unique")
        else:
            record_by_trial[trial_id] = record
        if not spec:
            errors.append(f"rq3: history {trial_id} has an unknown contrast")
            continue
        if record.get("campaign") != spec["campaign"] or record.get("property") != spec["property"]:
            errors.append(f"rq3: history {trial_id} has a mismatched campaign or property")
        replicate = record.get("replicate")
        if (
            not isinstance(pair_id, str)
            or re.fullmatch(r"m[123]-r\d{2}", pair_id) is None
            or pair_id.split("-", maxsplit=1)[0] != contrast_id.lower()
            or type(replicate) is not int
            or not 1 <= replicate <= RQ3_REPETITIONS
            or pair_id != f"{contrast_id.lower()}-r{replicate:02d}"
        ):
            errors.append(f"rq3: history {trial_id} has an invalid matched-pair identity")
        configuration_id = record.get("configuration_id")
        ordinal = record.get("ordinal")
        expected_arm_order = (
            [spec["left_configuration"], spec["right_configuration"]]
            if type(replicate) is int and replicate % 2 == 1
            else [spec["right_configuration"], spec["left_configuration"]]
        )
        if type(ordinal) is not int or ordinal < 1:
            errors.append(f"rq3: history {trial_id} has an invalid ordinal")
        else:
            ordinal_key = (contrast_id, ordinal)
            if ordinal_key in ordinals:
                errors.append(f"rq3: {contrast_id} has a duplicated history ordinal")
            ordinals.add(ordinal_key)
            configuration_position = (
                expected_arm_order.index(configuration_id)
                if configuration_id in expected_arm_order
                else None
            )
            expected_ordinal = (
                (replicate - 1) * 2 + configuration_position + 1
                if type(replicate) is int and configuration_position is not None
                else None
            )
            if expected_ordinal is None or ordinal != expected_ordinal:
                errors.append(f"rq3: history {trial_id} does not follow the preregistered arm order")
        if not isinstance(configuration_id, str) or configuration_id not in spec["configurations"]:
            errors.append(f"rq3: history {trial_id} has an unexpected configuration")
        if record.get("outcome") == "HARNESS_ERROR" or record.get("runner_error") is not None:
            errors.append(f"rq3: history {trial_id} contains a harness or runner error")
        if not _is_sha256(record.get("history_hash")):
            errors.append(f"rq3: history {trial_id} has no valid history hash")
        relative_path = record.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            errors.append(f"rq3: history {trial_id} has no path")
            continue
        if relative_path in paths:
            errors.append(f"rq3: history path is duplicated: {relative_path}")
            continue
        paths.add(relative_path)
        history_path = Path(relative_path)
        if history_path.is_absolute():
            errors.append(f"rq3: history path must be repository-relative: {relative_path}")
            continue
        if history_path.name != f"{trial_id}.json" or history_path.parent.name != spec["campaign"]:
            errors.append(f"rq3: history path does not match trial ID and campaign: {relative_path}")
            continue
        resolved_path = (root / history_path).resolve()
        try:
            resolved_path.relative_to(raw_root.resolve())
        except ValueError:
            errors.append(f"rq3: history path escapes results/raw/rq3: {relative_path}")
            continue
        if not resolved_path.is_file():
            errors.append(f"rq3: history file is missing: {relative_path}")
            continue
        try:
            history_object = read_history(resolved_path)
            payload = history_object.to_dict()
            recomputed_outcome = check_history(history_object).outcome.value
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            errors.append(f"rq3: invalid history {relative_path}: {error}")
            continue
        if payload.get("schema_version") != "history.v1":
            errors.append(f"rq3: history {relative_path} has an unsupported schema")
        if payload.get("history_hash") != record.get("history_hash"):
            errors.append(f"rq3: history hash differs from campaign record: {relative_path}")
        if recomputed_outcome != record.get("outcome"):
            errors.append(f"rq3: history outcome differs from raw-derived checker result: {relative_path}")
        history_manifest = payload.get("manifest", {})
        if not isinstance(history_manifest, dict):
            errors.append(f"rq3: history {relative_path} manifest is invalid")
            continue
        expected_history_fields = {
            "trial_id": trial_id,
            "campaign_id": spec["campaign"],
            "configuration_id": configuration_id,
            "property": spec["property"],
            "seed": record.get("pair_seed"),
            "rq3_contrast_id": contrast_id,
            "rq3_pair_id": pair_id,
            "rq3_pair_seed": record.get("pair_seed"),
            "rq3_replicate": replicate,
            "rq3_protocol_id": "rq3-protocol.v2",
            "rq3_topology_plan": TOPOLOGY_PLANS[contrast_id].to_dict(),
        }
        for field, expected in expected_history_fields.items():
            if history_manifest.get(field) != expected:
                errors.append(f"rq3: history {relative_path} manifest field {field} does not match")
        if (
            history_manifest.get("runner_dirty") is not False
            or history_manifest.get("runner_commit") != manifest.get("runner_commit")
        ):
            errors.append(f"rq3: history {relative_path} has mismatched runner provenance")
        if record.get("seed") != record.get("pair_seed") or record.get("runner_commit") != manifest.get("runner_commit"):
            errors.append(f"rq3: history record {trial_id} has mismatched seed or runner provenance")
        for field in (
            "prediction_commit",
            "prediction_manifest_hash",
            "protocol_commit",
            "protocol_hash",
            "software_versions",
            "image_digest",
            "checker_version",
        ):
            if history_manifest.get(field) != provenance.get(field):
                errors.append(f"rq3: history {relative_path} has mismatched {field}")
        contrast_row = contrast_rows.get(contrast_id, {})
        _check_rq3_operation_captures(
            payload,
            history_path=history_path,
            contrast_id=contrast_id,
            capture_operations=contrast_row.get("capture_operations", []),
            errors=errors,
        )
        if isinstance(pair_id, str):
            records_by_pair.setdefault(pair_id, []).append(record)
            histories_by_pair.setdefault(pair_id, {})[str(configuration_id)] = payload

    expected_pairs = {
        f"{contrast_id.lower()}-r{replicate:02d}"
        for contrast_id in RQ3_CONTRASTS
        for replicate in range(1, RQ3_REPETITIONS + 1)
    }
    if set(records_by_pair) != expected_pairs:
        errors.append("rq3: pair IDs are incomplete or unexpected")
    expected_ordinals = set(range(1, RQ3_REPETITIONS * 2 + 1))
    for contrast_id in RQ3_CONTRASTS:
        actual_ordinals = {ordinal for contrast, ordinal in ordinals if contrast == contrast_id}
        if actual_ordinals != expected_ordinals:
            errors.append(f"rq3: {contrast_id} history ordinals are incomplete or unexpected")
    for pair_id, pair_records in records_by_pair.items():
        match = re.fullmatch(r"(m[123])-r(\d{2})", pair_id)
        if match is None:
            errors.append(f"rq3: invalid pair ID: {pair_id}")
            continue
        contrast_id = match.group(1).upper()
        spec = RQ3_CONTRASTS.get(contrast_id)
        if not spec:
            continue
        arm_configs = [item.get("configuration_id") for item in pair_records]
        seeds = [item.get("pair_seed") for item in pair_records]
        if (
            len(pair_records) != 2
            or any(not isinstance(config, str) for config in arm_configs)
            or set(arm_configs) != spec["configurations"]
        ):
            errors.append(f"rq3: {pair_id} must contain exactly its two registered configuration arms")
        if any(type(seed) is not int for seed in seeds) or (len(seeds) == 2 and seeds[0] != seeds[1]):
            errors.append(f"rq3: {pair_id} arms do not share one integer pair seed")
        for item in pair_records:
            if item.get("seed") != item.get("pair_seed") or item.get("replicate") != int(match.group(2)):
                errors.append(f"rq3: {pair_id} arms have mismatched seed or repetition metadata")

    pair_controls = manifest.get("pair_controls")
    declared_controls: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(pair_controls, list):
        errors.append("rq3: pair_controls must be a list")
    else:
        for control in pair_controls:
            if not isinstance(control, dict):
                errors.append("rq3: pair_controls contain a non-object entry")
                continue
            key = (str(control.get("contrast_id")), str(control.get("pair_id")))
            if key in declared_controls:
                errors.append(f"rq3: duplicate control record for {key[1]}")
            declared_controls[key] = control
    expected_control_keys = {
        (contrast_id, pair_id)
        for contrast_id in RQ3_CONTRASTS
        for pair_id in expected_pair_ids_by_contrast.get(contrast_id, [])
    }
    if set(declared_controls) != expected_control_keys:
        errors.append(
            f"rq3: pair_controls do not cover the {len(expected_control_keys)} preregistered pairs exactly"
        )

    recomputed_controls: dict[tuple[str, str], dict[str, Any]] = {}
    valid_pair_rows: dict[str, list[dict[str, Any]]] = {contrast_id: [] for contrast_id in RQ3_CONTRASTS}
    valid_pair_ids: dict[str, list[str]] = {contrast_id: [] for contrast_id in RQ3_CONTRASTS}
    route_matches: Counter[tuple[str, str]] = Counter()
    m2_setup_evidence_count = 0
    for contrast_id in RQ3_CONTRASTS:
        for pair_id in expected_pair_ids_by_contrast[contrast_id]:
            arms = histories_by_pair.get(pair_id, {})
            observed_control = pair_control(
                contrast_id,
                pair_id,
                arms,
                configurations=configurations,
            )
            key = (contrast_id, pair_id)
            recomputed_controls[key] = observed_control
            if declared_controls.get(key) != observed_control:
                errors.append(f"rq3: {pair_id} pair-control manifest differs from raw-history recomputation")
            if observed_control.get("control_valid") is not True:
                errors.append(f"rq3: {pair_id} is not control-valid: {observed_control.get('invalid_reasons')}")
                continue
            valid_pair_ids[contrast_id].append(pair_id)
            arm_payloads = {
                configuration_id: dict(history)
                for configuration_id, history in arms.items()
            }
            for configuration_id, history in arm_payloads.items():
                record = next(
                    (
                        item
                        for item in records_by_pair.get(pair_id, [])
                        if item.get("configuration_id") == configuration_id
                    ),
                    {},
                )
                history["result"] = record.get("outcome")
                history["source_path"] = record.get("path")
            valid_pair_rows[contrast_id].append(
                _row_for_pair(contrast_id, pair_id, arm_payloads, configurations)
            )

            for operation_id, expected_member in RQ3_EXPECTED_ROUTES[contrast_id].items():
                operation_by_arm = {
                    configuration_id: _operation(history, operation_id)
                    for configuration_id, history in arms.items()
                }
                addresses = {
                    configuration_id: (
                        operation.get("actual_server_address")
                        if isinstance(operation, dict)
                        else None
                    )
                    for configuration_id, operation in operation_by_arm.items()
                }
                observed_routes = observed_control.get("observed", {})
                member_routes = {
                    configuration_id: (
                        arm.get("observed", {}).get("routes", {}).get(operation_id)
                        if isinstance(arm, dict)
                        else None
                    )
                    for configuration_id, arm in observed_routes.items()
                }
                if (
                    set(addresses) != RQ3_CONTRASTS[contrast_id]["configurations"]
                    or any(not isinstance(address, str) or not address for address in addresses.values())
                    or len(set(addresses.values())) != 1
                    or set(member_routes.values()) != {expected_member}
                ):
                    errors.append(
                        f"rq3: {pair_id} {operation_id} routes do not match {expected_member} on both arms"
                    )
                else:
                    route_matches[(contrast_id, operation_id)] += 1

            if contrast_id == "M2":
                for arm in observed_control.get("observed", {}).values():
                    setup = arm.get("observed", {}).get("setup_write") if isinstance(arm, dict) else None
                    concern = setup.get("write_concern") if isinstance(setup, dict) else None
                    if (
                        isinstance(setup, dict)
                        and setup.get("command_started") is True
                        and setup.get("member") == "mongo3"
                        and isinstance(concern, dict)
                        and concern.get("w") == 1
                    ):
                        m2_setup_evidence_count += 1
                    else:
                        errors.append(f"rq3: {pair_id} is missing M2 setup W1 command evidence on mongo3")

    for contrast_id, expected_routes in RQ3_EXPECTED_ROUTES.items():
        for operation_id in expected_routes:
            if route_matches[(contrast_id, operation_id)] != RQ3_REPETITIONS:
                errors.append(
                    f"rq3: {contrast_id} {operation_id} route match must be "
                    f"{RQ3_REPETITIONS}/{RQ3_REPETITIONS} pairs"
                )
    if m2_setup_evidence_count != RQ3_REPETITIONS * 2:
        errors.append(
            f"rq3: M2 setup W1 command evidence must cover {RQ3_REPETITIONS * 2}/{RQ3_REPETITIONS * 2} arms"
        )
    for contrast_id, pair_ids in valid_pair_ids.items():
        if len(pair_ids) != RQ3_REPETITIONS:
            errors.append(
                f"rq3: {contrast_id} control-valid pairs must be "
                f"{RQ3_REPETITIONS}/{RQ3_REPETITIONS}"
            )

    analysis_root = root / "results/analysis/rq3"
    summary_path = analysis_root / "summary.json"
    selection_path = analysis_root / "selection-manifest.json"
    summary = _read_object(summary_path, errors)
    selection = _read_object(selection_path, errors)
    campaign_digest = _sha256(manifest_path)
    preflight_digest = _sha256(root / "results/raw/rq3-preflight.json")
    anchor_history_ids = [
        record.get("trial_id")
        for pair in anchor_manifest.get("pairs", [])
        if isinstance(pair, dict)
        for record in pair.get("histories", [])
        if isinstance(record, dict)
    ]
    if summary is not None:
        try:
            expected_historical_anchors = _summarize_historical_anchors(
                anchor_manifest, repository_root=root
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as error:
            errors.append(f"rq3: cannot derive historical anchor observations: {error}")
            expected_historical_anchors = None
        if (
            summary.get("schema_version") != "rq3-analysis.v2"
            or summary.get("protocol_id") != "rq3-protocol.v2"
            or summary.get("campaign_manifest") != manifest_path.relative_to(root).as_posix()
            or summary.get("campaign_manifest_sha256") != campaign_digest
            or summary.get("preflight_sha256") != preflight_digest
            or summary.get("anchor_manifest_sha256") != anchor_digest
            or summary.get("historical_anchors") != expected_historical_anchors
            or summary.get("repetitions_per_contrast") != RQ3_REPETITIONS
            or summary.get("selection_manifest") != selection_path.relative_to(root).as_posix()
        ):
            errors.append("rq3: analysis summary is not tied to the v2 campaign, rehearsal, and anchors")
        contrast_summaries = summary.get("contrast_summaries")
        if not isinstance(contrast_summaries, dict) or set(contrast_summaries) != set(RQ3_CONTRASTS):
            errors.append("rq3: analysis summary must contain M1, M2, and M3")
        else:
            for contrast_id, contrast_summary in contrast_summaries.items():
                if not isinstance(contrast_summary, dict):
                    errors.append(f"rq3: analysis summary {contrast_id} is invalid")
                    continue
                if (
                    contrast_summary.get("planned_pair_count") != RQ3_REPETITIONS
                    or contrast_summary.get("control_valid_pair_count") != RQ3_REPETITIONS
                    or contrast_summary.get("invalid_pair_count") != 0
                    or contrast_summary.get("invalid_pairs") != []
                ):
                    errors.append(f"rq3: analysis summary {contrast_id} has incorrect control-valid counts")
                try:
                    raw_counts = _counts(valid_pair_rows[contrast_id], contrast_id)
                except (KeyError, TypeError, ValueError) as error:
                    errors.append(f"rq3: cannot derive {contrast_id} signature counts from raw histories: {error}")
                else:
                    if contrast_summary.get("signature_counts") != raw_counts:
                        errors.append(f"rq3: {contrast_id} signature counts differ from valid raw histories")
    if selection is not None:
        if (
            selection.get("schema_version") != "rq3-selection.v2"
            or selection.get("protocol_id") != "rq3-protocol.v2"
            or selection.get("campaign_manifest") != manifest_path.relative_to(root).as_posix()
            or selection.get("campaign_manifest_sha256") != campaign_digest
            or selection.get("preflight_sha256") != preflight_digest
            or selection.get("anchor_manifest_sha256") != anchor_digest
            or selection.get("anchor_history_ids") != anchor_history_ids
        ):
            errors.append("rq3: selection manifest is not tied to the v2 campaign, rehearsal, and anchors")
        selected_pairs = selection.get("selected_pairs")
        if not isinstance(selected_pairs, dict) or set(selected_pairs) != set(RQ3_CONTRASTS):
            errors.append("rq3: selection manifest must select one valid pair for M1, M2, and M3")
        else:
            contrast_summaries = summary.get("contrast_summaries") if summary is not None else None
            for contrast_id, selected in selected_pairs.items():
                expected_pair_id = min(valid_pair_ids.get(contrast_id, []), default=None)
                if not isinstance(selected, dict) or expected_pair_id is None:
                    errors.append(f"rq3: {contrast_id} selection is invalid")
                    continue
                if selected.get("pair_id") != expected_pair_id or selected.get("control_valid") is not True:
                    errors.append(f"rq3: {contrast_id} selection is not the first control-valid pair")
                selected_records = records_by_pair.get(expected_pair_id, [])
                histories = selected.get("histories")
                selected_by_config = {
                    item.get("configuration_id"): item
                    for item in histories
                    if isinstance(item, dict) and isinstance(item.get("configuration_id"), str)
                } if isinstance(histories, list) else {}
                if (
                    len(selected_records) != 2
                    or set(selected_by_config) != RQ3_CONTRASTS[contrast_id]["configurations"]
                ):
                    errors.append(f"rq3: {contrast_id} selection does not identify both campaign arms")
                    continue
                for record in selected_records:
                    selected_history = selected_by_config.get(record.get("configuration_id"), {})
                    if (
                        selected_history.get("trial_id") != record.get("trial_id")
                        or selected_history.get("sha256") != record.get("history_hash")
                        or selected_history.get("path") != record.get("path")
                    ):
                        errors.append(f"rq3: {contrast_id} selected history is not tied to its raw record")
                if isinstance(contrast_summaries, dict):
                    contrast_summary = contrast_summaries.get(contrast_id)
                    if (
                        not isinstance(contrast_summary, dict)
                        or contrast_summary.get("selected_pair") != selected
                    ):
                        errors.append(f"rq3: {contrast_id} analysis selection differs from its selection manifest")



def check_release_readiness(root: Path = ROOT) -> list[str]:
    """Return all reasons the current checkout must not be published as a release."""

    errors: list[str] = []
    for campaign, expected_count in EXPECTED_CAMPAIGNS.items():
        _check_campaign(root, campaign, expected_count, errors)
    _check_rq3(root, errors)

    summary = _read_object(root / "results/summary/summary.json", errors)
    if summary is not None:
        if summary.get("status") != "DATA":
            errors.append("analysis summary status must be DATA")
        campaigns = summary.get("campaign_summaries", {})
        if not isinstance(campaigns, dict):
            errors.append("analysis summary campaign_summaries must be an object")
        else:
            for campaign, expected_count in EXPECTED_SUMMARY_COUNTS.items():
                record = campaigns.get(campaign)
                actual = record.get("history_count") if isinstance(record, dict) else None
                if actual != expected_count:
                    errors.append(
                        f"analysis summary: {campaign} must contain {expected_count} histories"
                    )
        rq2_episodes = summary.get("fault_episode_summaries")
        if not isinstance(rq2_episodes, list):
            errors.append("analysis summary: fault_episode_summaries must be a list")
        else:
            episode_counts = {
                episode.get("topology_condition"): episode.get("episode_count")
                for episode in rq2_episodes
                if isinstance(episode, dict)
            }
            if len(rq2_episodes) != len(RQ2_EPISODE_COUNTS) or episode_counts != RQ2_EPISODE_COUNTS:
                errors.append("analysis summary: RQ2 must summarize 8/8/20 episodes for F1/F2/F3")

        groups = summary.get("groups")
        rq2_groups = [
            group
            for group in groups
            if isinstance(group, dict) and group.get("campaign_id") == "rq2"
        ] if isinstance(groups, list) else []
        if len(rq2_groups) != 48:
            errors.append("analysis summary: RQ2 must contain 48 condition/configuration/property groups")
        group_cells = {
            (
                group.get("topology_condition"),
                group.get("configuration_id"),
                group.get("property"),
            )
            for group in rq2_groups
        }
        expected_group_cells = {
            (condition, configuration_id, property_name)
            for condition in RQ2_CONDITIONS
            for property_name, configurations in EXPECTED_RQ2_CELLS.items()
            for configuration_id in configurations
        }
        if group_cells != expected_group_cells:
            errors.append("analysis summary: RQ2 condition/configuration/property groups are incomplete")
        for group in rq2_groups:
            baseline = group.get("normal_baseline")
            if (
                not isinstance(baseline, dict)
                or baseline.get("campaign_id") != "experiment"
                or baseline.get("history_count") != 3
            ):
                errors.append(
                    "analysis summary: every RQ2 group must carry its separate 3-history RQ1 normal baseline"
                )

    try:
        metadata = parse_metadata(root / "submission/metadata.mk")
    except BuildError as error:
        errors.append(str(error))
    else:
        members = [member.strip() for member in metadata["TEAM_MEMBERS"].split(";")]
        if len(members) != 3:
            errors.append("submission metadata must list exactly three team members")
        student_id = re.compile(r"\b[A-Z]\d{7}[A-Z]\b")
        for member in members:
            if not student_id.search(member) or re.search(r"pending|tbd|unknown", member, re.IGNORECASE):
                errors.append(f"submission metadata has a missing student ID: {member}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = check_release_readiness(args.root.resolve())
    if errors:
        print("Release readiness check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Release inputs are complete and ready to package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
