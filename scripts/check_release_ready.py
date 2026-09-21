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
EXPECTED_CAMPAIGNS = {"pilot": 192, "experiment": 1280, "rq2": 432}
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
EXPECTED_SUMMARY_COUNTS = {"pilot": 192, "experiment": 1280, "rq2": 432}
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
                    else ((False, 10), (True, 30))
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


def _check_rq3(root: Path, errors: list[str]) -> None:
    """Require complete, hash-linked RQ3 histories and report artifacts."""

    raw_root = root / "results/raw/rq3"
    manifest_path = raw_root / "campaign-manifest.json"
    manifest = _read_object(manifest_path, errors)
    if manifest is None:
        return
    if manifest.get("schema_version") != "rq3-campaign.v1" or manifest.get("campaign") != "rq3":
        errors.append("rq3: campaign manifest schema or campaign identifier is invalid")
    if manifest.get("status") != "COMPLETE":
        errors.append("rq3: campaign status must be COMPLETE")
    repetitions = manifest.get("repetitions_per_contrast")
    if type(repetitions) is not int or not 5 <= repetitions <= 10:
        errors.append("rq3: repetitions_per_contrast must be between 5 and 10")
        repetitions = 0
    expected_count = repetitions * 6
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
    for field in ("runner_script_sha256", "configuration_sha256", "protocol_sha256"):
        if not _is_sha256(manifest.get(field)):
            errors.append(f"rq3: {field} must be a SHA-256 digest")
    expected_frozen_files = {
        "runner_script_sha256": root / "scripts/run_rq3_campaign.py",
        "configuration_sha256": root / "configs/configurations.json",
        "protocol_sha256": root / "docs/experimental-protocol.md",
    }
    for field, path in expected_frozen_files.items():
        actual = _sha256(path)
        if actual is None or manifest.get(field) != actual:
            errors.append(f"rq3: {field} does not match {path.relative_to(root).as_posix()}")
    if provenance.get("protocol_hash") != manifest.get("protocol_sha256"):
        errors.append("rq3: protocol hash differs from the frozen protocol digest")

    registered_contrasts = manifest.get("contrasts")
    contrast_rows = {
        row.get("contrast_id"): row
        for row in registered_contrasts
        if isinstance(row, dict) and isinstance(row.get("contrast_id"), str)
    } if isinstance(registered_contrasts, list) else {}
    contrast_count = len(registered_contrasts) if isinstance(registered_contrasts, list) else 0
    if set(contrast_rows) != set(RQ3_CONTRASTS) or contrast_count != len(RQ3_CONTRASTS):
        errors.append("rq3: campaign must register exactly M1, M2, and M3")
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
        pair_ids = row.get("pair_ids")
        expected_pair_ids = [f"{contrast_id.lower()}-r{rep:02d}" for rep in range(1, repetitions + 1)]
        if pair_ids != expected_pair_ids:
            errors.append(f"rq3: {contrast_id} pair_ids do not match the registered repetitions")

    records = manifest.get("records")
    if not isinstance(records, list) or len(records) != expected_count:
        errors.append(f"rq3: expected {expected_count} history records")
        return
    record_by_trial: dict[str, dict[str, Any]] = {}
    records_by_pair: dict[str, list[dict[str, Any]]] = {}
    paths: set[str] = set()
    ordinals: set[tuple[str, int]] = set()
    source_root = Path(__file__).resolve().parents[1] / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        from mongo_consistency.history import read_history
    except ImportError as error:
        errors.append(f"rq3: cannot load history validator: {error}")
        return

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
        elif isinstance(record, dict):
            record_by_trial[trial_id] = record
        if not spec:
            errors.append(f"rq3: history {trial_id} has an unknown contrast")
            continue
        if record.get("campaign") != spec["campaign"] or record.get("property") != spec["property"]:
            errors.append(f"rq3: history {trial_id} has a mismatched campaign or property")
        if (
            not isinstance(pair_id, str)
            or re.fullmatch(r"m[123]-r\d{2}", pair_id) is None
            or pair_id.split("-", maxsplit=1)[0] != contrast_id.lower()
            or type(record.get("replicate")) is not int
            or not 1 <= record["replicate"] <= repetitions
            or pair_id != f"{contrast_id.lower()}-r{record.get('replicate', 0):02d}"
        ):
            errors.append(f"rq3: history {trial_id} has an invalid matched-pair identity")
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal < 1:
            errors.append(f"rq3: history {trial_id} has an invalid ordinal")
        else:
            ordinal_key = (contrast_id, ordinal)
            if ordinal_key in ordinals:
                errors.append(f"rq3: {contrast_id} has a duplicated history ordinal")
            ordinals.add(ordinal_key)
        if (
            not isinstance(record.get("configuration_id"), str)
            or record.get("configuration_id") not in spec["configurations"]
        ):
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
            history = read_history(resolved_path)
            payload = history.to_dict()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            errors.append(f"rq3: invalid history {relative_path}: {error}")
            continue
        if payload.get("schema_version") != "history.v1":
            errors.append(f"rq3: history {relative_path} has an unsupported schema")
        if payload.get("history_hash") != record.get("history_hash"):
            errors.append(f"rq3: history hash differs from campaign record: {relative_path}")
        history_manifest = payload.get("manifest", {})
        if not isinstance(history_manifest, dict):
            errors.append(f"rq3: history {relative_path} manifest is invalid")
            continue
        expected_history_fields = {
            "trial_id": trial_id,
            "campaign_id": spec["campaign"],
            "configuration_id": record.get("configuration_id"),
            "property": spec["property"],
            "seed": record.get("pair_seed"),
            "rq3_contrast_id": contrast_id,
            "rq3_pair_id": pair_id,
            "rq3_pair_seed": record.get("pair_seed"),
            "rq3_replicate": record.get("replicate"),
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
        capture_operations = contrast_row.get("capture_operations", [])
        _check_rq3_operation_captures(
            payload,
            history_path=history_path,
            contrast_id=contrast_id,
            capture_operations=capture_operations,
            errors=errors,
        )
        if isinstance(pair_id, str):
            records_by_pair.setdefault(pair_id, []).append(record)

    expected_pairs = {
        f"{contrast_id.lower()}-r{replicate:02d}"
        for contrast_id in RQ3_CONTRASTS
        for replicate in range(1, repetitions + 1)
    }
    if set(records_by_pair) != expected_pairs:
        errors.append("rq3: pair IDs are incomplete or unexpected")
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

    analysis_root = root / "results/analysis/rq3"
    summary_path = analysis_root / "summary.json"
    selection_path = analysis_root / "selection-manifest.json"
    summary = _read_object(summary_path, errors)
    selection = _read_object(selection_path, errors)
    campaign_digest = _sha256(manifest_path)
    if summary is not None:
        if summary.get("schema_version") != "rq3-analysis.v1":
            errors.append("rq3: analysis summary schema is invalid")
        if (
            summary.get("campaign_manifest") != manifest_path.relative_to(root).as_posix()
            or summary.get("campaign_manifest_sha256") != campaign_digest
            or summary.get("repetitions_per_contrast") != repetitions
            or summary.get("selection_manifest") != selection_path.relative_to(root).as_posix()
        ):
            errors.append("rq3: analysis summary is not tied to the complete campaign manifest")
        generated_artifacts = summary.get("generated_artifacts")
        expected_artifacts = {
            "report_tex": "submission/generated-rq3.tex",
            "timeline_pdf": "submission/figures/rq3-causal-timeline.pdf",
        }
        if not isinstance(generated_artifacts, dict) or set(generated_artifacts) != set(expected_artifacts):
            errors.append("rq3: analysis summary must bind the generated report TeX and timeline PDF")
        else:
            for artifact_name, relative_path in expected_artifacts.items():
                entry = generated_artifacts.get(artifact_name)
                if not isinstance(entry, dict) or entry.get("path") != relative_path:
                    errors.append(f"rq3: analysis summary has an invalid {artifact_name} artifact path")
                    continue
                artifact_digest = _sha256(root / relative_path)
                if not _is_sha256(entry.get("sha256")) or artifact_digest != entry.get("sha256"):
                    errors.append(f"rq3: {artifact_name} differs from its analysis summary digest")
        contrast_summaries = summary.get("contrast_summaries")
        if not isinstance(contrast_summaries, dict) or set(contrast_summaries) != set(RQ3_CONTRASTS):
            errors.append("rq3: analysis summary must contain M1, M2, and M3")
        else:
            for contrast_id, contrast_summary in contrast_summaries.items():
                if not isinstance(contrast_summary, dict) or contrast_summary.get("pair_count") != repetitions:
                    errors.append(f"rq3: analysis summary {contrast_id} must include every matched pair")
                elif not isinstance(contrast_summary.get("signature_counts"), dict):
                    errors.append(f"rq3: analysis summary {contrast_id} has no signature counts")
    if selection is not None:
        if (
            selection.get("schema_version") != "rq3-selection.v1"
            or selection.get("campaign_manifest") != manifest_path.relative_to(root).as_posix()
            or selection.get("campaign_manifest_sha256") != campaign_digest
        ):
            errors.append("rq3: selection manifest is not tied to the complete campaign manifest")
        selected_pairs = selection.get("selected_pairs")
        if not isinstance(selected_pairs, dict) or set(selected_pairs) != set(RQ3_CONTRASTS):
            errors.append("rq3: selection manifest must select one pair for M1, M2, and M3")
        else:
            for contrast_id, selected in selected_pairs.items():
                if not isinstance(selected, dict):
                    errors.append(f"rq3: {contrast_id} selection is invalid")
                    continue
                pair_id = selected.get("pair_id")
                histories = selected.get("histories")
                expected_pair = records_by_pair.get(pair_id, []) if isinstance(pair_id, str) else []
                selected_by_config = {}
                if isinstance(histories, list):
                    for item in histories:
                        if not isinstance(item, dict) or not isinstance(item.get("configuration_id"), str):
                            continue
                        selected_by_config[item["configuration_id"]] = item
                if len(expected_pair) != 2 or set(selected_by_config) != RQ3_CONTRASTS[contrast_id]["configurations"]:
                    errors.append(f"rq3: {contrast_id} selection does not identify both campaign arms")
                    continue
                for record in expected_pair:
                    selected_history = selected_by_config.get(record.get("configuration_id"), {})
                    if (
                        selected_history.get("trial_id") != record.get("trial_id")
                        or selected_history.get("sha256") != record.get("history_hash")
                        or selected_history.get("path") != record.get("path")
                    ):
                        errors.append(f"rq3: {contrast_id} selected history is not tied to its campaign record")
                if summary is not None:
                    summaries = summary.get("contrast_summaries")
                    contrast_summary = summaries.get(contrast_id) if isinstance(summaries, dict) else None
                    if (
                        not isinstance(contrast_summary, dict)
                        or contrast_summary.get("selected_pair") != selected
                    ):
                        errors.append(f"rq3: analysis summary {contrast_id} selection differs from the selection manifest")

    generated_tex = root / "submission/generated-rq3.tex"
    try:
        tex = generated_tex.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(f"rq3: generated report TeX is missing or unreadable: {error}")
    else:
        for marker in (
            r"\subsection{Mechanism contrasts (RQ3)}",
            r"\label{tab:rq3-mechanisms}",
            r"\maybefigure[fig:rq3-causal-timeline]{submission/figures/rq3-causal-timeline.pdf}",
        ):
            if marker not in tex:
                errors.append(f"rq3: generated report TeX is missing {marker}")
    results_tex = root / "submission/sections/07-results.tex"
    try:
        results_source = results_tex.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(f"rq3: results section is missing or unreadable: {error}")
    else:
        if r"\input{submission/generated-rq3.tex}" not in results_source:
            errors.append("rq3: generated report TeX is not included in the results section")
    timeline = root / "submission/figures/rq3-causal-timeline.pdf"
    try:
        with timeline.open("rb") as handle:
            header = handle.read(8)
        if not header.startswith(b"%PDF-") or timeline.stat().st_size < 500:
            errors.append("rq3: generated causal timeline is not a valid PDF artifact")
    except OSError as error:
        errors.append(f"rq3: generated causal timeline PDF is missing or unreadable: {error}")


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
                or baseline.get("history_count") != 10
            ):
                errors.append(
                    "analysis summary: every RQ2 group must carry its separate 10-history RQ1 normal baseline"
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
