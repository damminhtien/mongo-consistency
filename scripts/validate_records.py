"""Validate committed record formats and any generated experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations, load_json, load_predictions
from mongo_consistency.history import read_history
from mongo_consistency.models import Outcome

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAMES = (
    "campaign-run.v1.json",
    "rq3-campaign.v1.json",
    "manifest.v1.json",
    "operation.v1.json",
    "fault-event.v1.json",
    "history.v1.json",
    "outcome.v1.json",
    "summary.v1.json",
)
RQ2_HISTORY_COUNT = 432
RQ2_CONDITIONS = {"F1", "F2", "F3"}
RQ2_CONFIGURATIONS = {"C1", "C3", "C4", "C6"}
RQ2_PROPERTIES = {"RYW", "MR", "MW", "WFR"}
RQ2_SIGNATURE_CELLS = {("C1", "RYW"), ("C6", "RYW"), ("C1", "MW"), ("C6", "MW")}
RQ2_EPISODE_IDS = {
    f"rq2-{condition.lower()}-r{repetition:02d}"
    for repetition in range(1, 9)
    for condition in RQ2_CONDITIONS
} | {f"rq2-f3-r{repetition:02d}" for repetition in range(9, 21)}
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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _load_validators(root: Path, errors: list[str]) -> dict[str, Draft202012Validator]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    schema_ids: set[str] = set()

    for schema_name in SCHEMA_NAMES:
        path = root / "schemas" / schema_name
        try:
            payload = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{path}: cannot read schema: {error}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path}: schema must be an object")
            continue
        try:
            Draft202012Validator.check_schema(payload)
        except SchemaError as error:
            errors.append(f"{path}: invalid Draft 2020-12 schema: {error.message}")
            continue
        schema_id = payload.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            errors.append(f"{path}: schema must define a non-empty $id")
            continue
        if schema_id in schema_ids:
            errors.append(f"{path}: duplicate schema $id {schema_id!r}")
            continue
        schema_ids.add(schema_id)
        schemas[schema_name] = payload
        resources.append((schema_id, Resource.from_contents(payload)))

    registry = Registry().with_resources(resources)
    validators: dict[str, Draft202012Validator] = {}
    for schema_name, schema in schemas.items():
        validators[schema_name] = Draft202012Validator(schema, registry=registry)
    return validators


def _check_schema_instance(
    instance: Any,
    validator: Draft202012Validator,
    location: str,
    errors: list[str],
) -> None:
    validation_errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    for error in validation_errors:
        instance_path = ".".join(str(part) for part in error.absolute_path)
        suffix = f".{instance_path}" if instance_path else ""
        errors.append(f"{location}{suffix}: {error.message}")


def _check_manifest(manifest: Any, location: str, errors: list[str]) -> None:
    _require(isinstance(manifest, dict), f"{location}: expected an object", errors)
    if not isinstance(manifest, dict):
        return
    required = (
        "schema_version",
        "trial_id",
        "campaign_id",
        "configuration_id",
        "read_concern",
        "write_concern",
        "causal_session",
        "property",
        "schedule_id",
        "seed",
        "namespace",
        "timeout_policy",
    )
    for field in required:
        _require(field in manifest, f"{location}: missing {field}", errors)
    _require(manifest.get("schema_version") == "manifest.v1", f"{location}: schema_version must be manifest.v1", errors)
    campaign_id = manifest.get("campaign_id")
    configuration_id = manifest.get("configuration_id")
    property_name = manifest.get("property")
    _require(
        isinstance(campaign_id, str)
        and campaign_id in {"smoke", "pilot", "normal", "experiment", "rq2", "rq3-m1", "rq3-m2", "rq3-m3"},
        f"{location}: invalid campaign_id",
        errors,
    )
    _require(
        isinstance(configuration_id, str)
        and configuration_id in {f"C{number}" for number in range(1, 9)},
        f"{location}: invalid configuration_id",
        errors,
    )
    _require(
        isinstance(property_name, str) and property_name in {"RYW", "MR", "MW", "WFR"},
        f"{location}: invalid property",
        errors,
    )
    _require(isinstance(manifest.get("causal_session"), bool), f"{location}: causal_session must be boolean", errors)
    _require(isinstance(manifest.get("namespace"), dict), f"{location}: namespace must be an object", errors)
    _require(isinstance(manifest.get("timeout_policy"), dict), f"{location}: timeout_policy must be an object", errors)
    if manifest.get("campaign_id") == "rq2":
        condition = manifest.get("topology_condition")
        repetition = manifest.get("fault_repetition")
        configuration_id = manifest.get("configuration_id")
        property_name = manifest.get("property")
        valid_condition = isinstance(condition, str) and condition in RQ2_CONDITIONS
        valid_configuration = (
            isinstance(configuration_id, str) and configuration_id in RQ2_CONFIGURATIONS
        )
        valid_property = isinstance(property_name, str) and property_name in RQ2_PROPERTIES
        _require(valid_condition, f"{location}: invalid RQ2 topology_condition", errors)
        _require(
            valid_configuration,
            f"{location}: invalid RQ2 configuration_id",
            errors,
        )
        _require(valid_property, f"{location}: invalid RQ2 property", errors)
        _require(
            isinstance(repetition, int) and not isinstance(repetition, bool) and repetition >= 1,
            f"{location}: invalid RQ2 fault_repetition",
            errors,
        )
        expected_episode_id = (
            f"rq2-{condition.lower()}-r{repetition:02d}"
            if valid_condition
            and isinstance(repetition, int)
            and not isinstance(repetition, bool)
            else None
        )
        _require(
            manifest.get("fault_episode_id") == expected_episode_id,
            f"{location}: fault_episode_id does not match condition/repetition",
            errors,
        )
        _require(
            isinstance(manifest.get("fault_event_id"), str)
            and bool(manifest.get("fault_event_id")),
            f"{location}: RQ2 fault_event_id is required",
            errors,
        )
        signature = (
            condition == "F3"
            and valid_configuration
            and valid_property
            and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
            and isinstance(repetition, int)
            and not isinstance(repetition, bool)
            and 9 <= repetition <= 20
        )
        _require(
            manifest.get("signature_extension") is signature,
            f"{location}: signature_extension does not match the registered plan",
            errors,
        )
    elif isinstance(manifest.get("campaign_id"), str) and manifest["campaign_id"].startswith("rq3-"):
        contrast_id = manifest.get("rq3_contrast_id")
        expected = {
            "M1": ("rq3-m1", {"C5", "C6"}, "RYW"),
            "M2": ("rq3-m2", {"C8", "C5"}, "WFR"),
            "M3": ("rq3-m3", {"C3", "C6"}, "MW"),
        }.get(contrast_id) if isinstance(contrast_id, str) else None
        _require(expected is not None, f"{location}: invalid RQ3 contrast id", errors)
        if expected is not None:
            campaign, configurations, property_name = expected
            _require(manifest.get("campaign_id") == campaign, f"{location}: RQ3 campaign does not match contrast", errors)
            _require(
                isinstance(configuration_id, str) and configuration_id in configurations,
                f"{location}: RQ3 configuration does not match contrast",
                errors,
            )
            _require(manifest.get("property") == property_name, f"{location}: RQ3 property does not match contrast", errors)
        _require(manifest.get("adversarial") is True, f"{location}: RQ3 history must use the fault schedule", errors)
        _require(manifest.get("seed") == manifest.get("rq3_pair_seed"), f"{location}: RQ3 seed differs from pair seed", errors)
        _require(isinstance(manifest.get("rq3_pair_id"), str), f"{location}: RQ3 pair id is required", errors)
        _require(isinstance(manifest.get("rq3_replicate"), int) and not isinstance(manifest.get("rq3_replicate"), bool), f"{location}: RQ3 replicate is required", errors)
        _require(isinstance(manifest.get("topology_capture_operations"), list), f"{location}: RQ3 topology capture operations are required", errors)


def _check_campaign_manifest(
    path: Path,
    errors: list[str],
    validators: dict[str, Draft202012Validator],
) -> None:
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return
    _require(isinstance(payload, dict), f"{path}: expected an object", errors)
    if not isinstance(payload, dict):
        return
    if payload.get("schema_version") == "rq3-campaign.v1":
        validator = validators.get("rq3-campaign.v1.json")
        if validator is not None:
            _check_schema_instance(payload, validator, str(path), errors)
        _check_rq3_campaign_manifest(payload, path, errors)
        return
    validator = validators.get("campaign-run.v1.json")
    if validator is not None:
        previous_error_count = len(errors)
        _check_schema_instance(payload, validator, str(path), errors)
        if len(errors) != previous_error_count:
            return
    _require(payload.get("schema_version") == "campaign-run.v1", f"{path}: invalid schema_version", errors)
    if "status" in payload:
        _require(
            payload.get("status") in {"RUNNING", "COMPLETE", "INTERRUPTED", "FAILED"},
            f"{path}: invalid status",
            errors,
        )
    expected_count = payload.get("expected_case_count")
    if "expected_case_count" in payload:
        _require(
            isinstance(expected_count, int)
            and not isinstance(expected_count, bool)
            and expected_count >= 0,
            f"{path}: expected_case_count must be a non-negative integer",
            errors,
        )
    records = payload.get("records")
    _require(isinstance(records, list), f"{path}: records must be a list", errors)
    if isinstance(records, list):
        _require(payload.get("case_count") == len(records), f"{path}: case_count does not match records", errors)
        if "completed_case_count" in payload:
            _require(
                payload.get("completed_case_count") == len(records),
                f"{path}: completed_case_count does not match records",
                errors,
            )
        ordinals: list[int] = []
        for index, record in enumerate(records):
            _require(isinstance(record, dict), f"{path}: records[{index}] must be an object", errors)
            if isinstance(record, dict):
                for field in ("trial_id", "campaign", "configuration_id", "property", "seed", "history_hash"):
                    _require(field in record, f"{path}: records[{index}] missing {field}", errors)
                if "ordinal" in record:
                    ordinal = record.get("ordinal")
                    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                        ordinals.append(ordinal)
                    else:
                        _require(False, f"{path}: records[{index}] ordinal must be an integer", errors)
        planned = payload.get("planned_ordinals")
        if planned is not None:
            _require(
                isinstance(planned, list) and all(
                    isinstance(ordinal, int) and not isinstance(ordinal, bool)
                    for ordinal in planned
                ),
                f"{path}: planned_ordinals must be an integer list",
                errors,
            )
            if isinstance(planned, list):
                if isinstance(expected_count, int) and not isinstance(expected_count, bool):
                    _require(
                        expected_count == len(planned),
                        f"{path}: expected_case_count does not match planned_ordinals",
                        errors,
                    )
                _require(
                    len(ordinals) == len(set(ordinals)) and set(ordinals).issubset(set(planned)),
                    f"{path}: records contain duplicate or unplanned ordinals",
                    errors,
                )
    if payload.get("campaign") == "rq2":
        _require(
            expected_count == RQ2_HISTORY_COUNT,
            f"{path}: RQ2 expected_case_count must be {RQ2_HISTORY_COUNT}",
            errors,
        )
        planned_episode_ids = payload.get("planned_episode_ids")
        valid_planned_ids = (
            isinstance(planned_episode_ids, list)
            and all(isinstance(episode_id, str) for episode_id in planned_episode_ids)
        )
        _require(
            valid_planned_ids
            and len(planned_episode_ids) == len(RQ2_EPISODE_IDS)
            and set(planned_episode_ids) == RQ2_EPISODE_IDS,
            f"{path}: RQ2 planned episodes do not match the registered 36 episodes",
            errors,
        )
        episode_entries = payload.get("episodes")
        _require(isinstance(episode_entries, list), f"{path}: RQ2 episodes must be a list", errors)
        if isinstance(episode_entries, list):
            episode_ids = [
                episode.get("episode_id")
                for episode in episode_entries
                if isinstance(episode, dict)
                and isinstance(episode.get("episode_id"), str)
            ]
            _require(
                len(episode_ids) == len(episode_entries)
                and len(episode_ids) == len(set(episode_ids))
                and set(episode_ids).issubset(RQ2_EPISODE_IDS),
                f"{path}: RQ2 episodes contain duplicate or unknown episode IDs",
                errors,
            )
            if payload.get("status") == "COMPLETE":
                _require(
                    len(episode_entries) == len(RQ2_EPISODE_IDS)
                    and set(episode_ids) == RQ2_EPISODE_IDS
                    and payload.get("fault_episode_count") == len(RQ2_EPISODE_IDS)
                    and payload.get("completed_fault_episode_count") == len(RQ2_EPISODE_IDS),
                    f"{path}: complete RQ2 campaign must record all 36 episodes",
                    errors,
                )
        if isinstance(records, list):
            records_by_episode: dict[str, list[dict[str, Any]]] = {}
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                condition = record.get("topology_condition")
                repetition = record.get("repetition")
                configuration_id = record.get("configuration_id")
                property_name = record.get("property")
                valid_condition = isinstance(condition, str) and condition in RQ2_CONDITIONS
                valid_configuration = (
                    isinstance(configuration_id, str)
                    and configuration_id in RQ2_CONFIGURATIONS
                )
                valid_property = isinstance(property_name, str) and property_name in RQ2_PROPERTIES
                expected_episode_id = (
                    f"rq2-{condition.lower()}-r{repetition:02d}"
                    if valid_condition
                    and isinstance(repetition, int)
                    and not isinstance(repetition, bool)
                    else None
                )
                for field in (
                    "topology_condition",
                    "fault_episode_id",
                    "repetition",
                    "signature_extension",
                ):
                    _require(field in record, f"{path}: records[{index}] missing RQ2 {field}", errors)
                _require(
                    valid_condition and valid_configuration and valid_property,
                    f"{path}: records[{index}] has an invalid RQ2 cell",
                    errors,
                )
                _require(
                    expected_episode_id in RQ2_EPISODE_IDS
                    and record.get("fault_episode_id") == expected_episode_id,
                    f"{path}: records[{index}] fault_episode_id does not match",
                    errors,
                )
                is_signature = (
                    condition == "F3"
                    and valid_configuration
                    and valid_property
                    and (configuration_id, property_name) in RQ2_SIGNATURE_CELLS
                    and isinstance(repetition, int)
                    and not isinstance(repetition, bool)
                    and 9 <= repetition <= 20
                )
                _require(
                    record.get("signature_extension") is is_signature,
                    f"{path}: records[{index}] signature_extension does not match",
                    errors,
                )
                if isinstance(expected_episode_id, str):
                    records_by_episode.setdefault(expected_episode_id, []).append(record)

            if isinstance(episode_entries, list):
                for index, episode in enumerate(episode_entries):
                    if not isinstance(episode, dict):
                        continue
                    episode_id = episode.get("episode_id")
                    expected = (
                        RQ2_EPISODE_PLAN.get(episode_id)
                        if isinstance(episode_id, str)
                        else None
                    )
                    if expected is None:
                        continue
                    event = episode.get("event")
                    _require(
                        episode.get("fault_status") == "APPLIED"
                        and episode.get("recovery_status") == "CONVERGED"
                        and episode.get("error") is None
                        and isinstance(event, dict)
                        and event.get("status") == "APPLIED"
                        and event.get("recovery_status") == "CONVERGED",
                        f"{path}: RQ2 episode {episode_id} did not verify fault application and recovery",
                        errors,
                    )
                    if expected["topology_condition"] == "F1":
                        _require(
                            isinstance(event, dict) and event.get("fault_verified") is True,
                            f"{path}: RQ2 episode {episode_id} did not verify the surviving topology",
                            errors,
                        )
                    if expected["topology_condition"] == "F3":
                        apply = event.get("coordinator_apply") if isinstance(event, dict) else None
                        details = apply.get("details") if isinstance(apply, dict) else None
                        controller = details.get("controller") if isinstance(details, dict) else None
                        _require(
                            isinstance(controller, dict)
                            and controller.get("verified") is True
                            and controller.get("replication_isolated") is True,
                            f"{path}: RQ2 episode {episode_id} lacks verified partition evidence",
                            errors,
                        )
                    episode_records = records_by_episode.get(episode_id, [])
                    record_ids = [record.get("trial_id") for record in episode_records]
                    listed_ids = episode.get("trial_ids")
                    _require(
                        episode.get("topology_condition") == expected["topology_condition"]
                        and episode.get("repetition") == expected["repetition"]
                        and episode.get("signature_extension") is expected["signature_extension"],
                        f"{path}: episodes[{index}] metadata does not match the registered plan",
                        errors,
                    )
                    _require(
                        episode.get("history_count") == expected["history_count"]
                        and len(episode_records) == expected["history_count"],
                        f"{path}: episodes[{index}] history count does not match its records",
                        errors,
                    )
                    _require(
                        isinstance(listed_ids, list)
                        and all(isinstance(trial_id, str) for trial_id in listed_ids)
                        and len(listed_ids) == len(set(listed_ids))
                        and all(isinstance(trial_id, str) for trial_id in record_ids)
                        and set(listed_ids) == set(record_ids),
                        f"{path}: episodes[{index}] trial_ids do not match the episode records",
                        errors,
                    )


def _check_rq3_campaign_manifest(
    payload: dict[str, Any], path: Path, errors: list[str]
) -> None:
    _require(payload.get("campaign") == "rq3", f"{path}: invalid RQ3 campaign name", errors)
    status = payload.get("status")
    _require(isinstance(status, str) and status in {"RUNNING", "COMPLETE", "INTERRUPTED", "FAILED"}, f"{path}: invalid RQ3 status", errors)
    repetitions = payload.get("repetitions_per_contrast")
    valid_repetitions = isinstance(repetitions, int) and not isinstance(repetitions, bool) and 5 <= repetitions <= 10
    _require(valid_repetitions, f"{path}: invalid RQ3 repetitions", errors)
    records = payload.get("records")
    _require(isinstance(records, list), f"{path}: RQ3 records must be a list", errors)
    if not isinstance(records, list):
        return
    _require(payload.get("completed_case_count") == len(records), f"{path}: RQ3 completed count does not match records", errors)
    if valid_repetitions:
        _require(payload.get("planned_case_count") == repetitions * 6, f"{path}: RQ3 plan must contain six histories per repetition", errors)
        if payload.get("status") == "COMPLETE":
            _require(len(records) == repetitions * 6, f"{path}: completed RQ3 campaign is missing histories", errors)
    _require(bool(payload.get("runner_commit")), f"{path}: RQ3 runner commit is missing", errors)
    provenance = payload.get("runtime_provenance")
    _require(isinstance(provenance, dict) and provenance.get("runner_dirty") is False, f"{path}: RQ3 runner provenance is missing or dirty", errors)
    if payload.get("status") != "COMPLETE":
        return
    expected = {"M1": {"C5", "C6"}, "M2": {"C8", "C5"}, "M3": {"C3", "C6"}}
    campaign_by_contrast = {"M1": "rq3-m1", "M2": "rq3-m2", "M3": "rq3-m3"}
    pairs: dict[str, list[dict[str, Any]]] = {}
    repository_root = next(
        (parent for parent in path.parents if (parent / "configs/configurations.json").is_file()),
        None,
    )
    for index, record in enumerate(records):
        _require(isinstance(record, dict), f"{path}: RQ3 record {index} must be an object", errors)
        if not isinstance(record, dict):
            continue
        pair_id = record.get("pair_id")
        contrast_id = record.get("contrast_id")
        valid_contrast = isinstance(contrast_id, str) and contrast_id in expected
        _require(valid_contrast, f"{path}: RQ3 record has invalid contrast", errors)
        _require(
            valid_contrast and record.get("campaign") == campaign_by_contrast[contrast_id],
            f"{path}: RQ3 record campaign does not match contrast",
            errors,
        )
        replicate = record.get("replicate")
        _require(
            valid_contrast
            and type(replicate) is int
            and 1 <= replicate <= repetitions
            and pair_id == f"{contrast_id.lower()}-r{replicate:02d}",
            f"{path}: RQ3 record has invalid pair identity",
            errors,
        )
        configuration_id = record.get("configuration_id")
        _require(
            valid_contrast
            and isinstance(configuration_id, str)
            and configuration_id in expected.get(contrast_id, set()),
            f"{path}: RQ3 record has invalid configuration",
            errors,
        )
        _require(record.get("seed") == record.get("pair_seed"), f"{path}: RQ3 record seed differs from its pair seed", errors)
        _require(record.get("outcome") != "HARNESS_ERROR" and record.get("runner_error") is None, f"{path}: RQ3 record contains a runner error", errors)
        relative_history_path = record.get("path")
        if repository_root is not None and isinstance(relative_history_path, str):
            relative_path = Path(relative_history_path)
            raw_root = repository_root / "results/raw/rq3"
            if relative_path.is_absolute():
                errors.append(f"{path}: RQ3 history path must be repository-relative: {relative_history_path}")
                continue
            history_path = repository_root / relative_path
            resolved_history_path = history_path.resolve()
            try:
                resolved_history_path.relative_to(raw_root.resolve())
            except ValueError:
                errors.append(f"{path}: RQ3 history path escapes results/raw/rq3: {relative_history_path}")
                continue
            if (
                relative_path.name != f"{record.get('trial_id')}.json"
                or not valid_contrast
                or relative_path.parent.name != campaign_by_contrast[contrast_id]
            ):
                errors.append(f"{path}: RQ3 history path does not match its record: {relative_history_path}")
                continue
            try:
                history = read_history(resolved_history_path)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as error:
                errors.append(f"{path}: cannot validate RQ3 history {record['path']}: {error}")
            else:
                _require(history.history_hash == record.get("history_hash"), f"{path}: RQ3 history hash differs for {record.get('trial_id')}", errors)
                _require(history.manifest.get("trial_id") == record.get("trial_id"), f"{path}: RQ3 history id differs for {record.get('trial_id')}", errors)
                _require(history.manifest.get("rq3_pair_id") == pair_id, f"{path}: RQ3 history pair id differs for {record.get('trial_id')}", errors)
        if isinstance(pair_id, str):
            pairs.setdefault(pair_id, []).append(record)
    if valid_repetitions:
        _require(len(pairs) == 3 * repetitions, f"{path}: RQ3 pair count is incomplete", errors)
    for pair_id, pair_records in pairs.items():
        configuration_values = [record.get("configuration_id") for record in pair_records]
        seed_values = [record.get("seed") for record in pair_records]
        _require(len(pair_records) == 2, f"{path}: {pair_id} must contain two arms", errors)
        _require(
            all(isinstance(value, str) for value in configuration_values)
            and any(set(configuration_values) == configs for configs in expected.values()),
            f"{path}: {pair_id} has the wrong configuration pair",
            errors,
        )
        _require(
            len(seed_values) == 2
            and all(type(value) is int for value in seed_values)
            and seed_values[0] == seed_values[1],
            f"{path}: {pair_id} arms must share an integer seed",
            errors,
        )


def _check_summary(
    path: Path,
    errors: list[str],
    validators: dict[str, Draft202012Validator],
) -> None:
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return
    _require(isinstance(payload, dict), f"{path}: expected an object", errors)
    if not isinstance(payload, dict):
        return
    validator = validators.get("summary.v1.json")
    if validator is not None:
        previous_error_count = len(errors)
        _check_schema_instance(payload, validator, str(path), errors)
        if len(errors) != previous_error_count:
            return
    _require(payload.get("schema_version") == "summary.v1", f"{path}: invalid schema_version", errors)
    _require(payload.get("status") in {"DATA", "NO_DATA"}, f"{path}: invalid status", errors)
    counts = payload.get("outcome_counts")
    _require(isinstance(counts, dict), f"{path}: outcome_counts must be an object", errors)
    if isinstance(counts, dict):
        for outcome in Outcome:
            _require(outcome.value in counts, f"{path}: outcome_counts missing {outcome.value}", errors)
            value = counts.get(outcome.value)
            _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, f"{path}: invalid count for {outcome.value}", errors)


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    validators = _load_validators(root, errors)

    try:
        load_configurations(root / "configs/configurations.json")
        load_predictions(root / "configs/predictions.json")
        load_json(root / "configs/campaign.json")
        load_json(root / "configs/schedules.json")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        errors.append(f"configuration files: {error}")

    raw_root = root / "results/raw"
    if raw_root.exists():
        for path in sorted(raw_root.rglob("*.json")):
            if ".rq2-staging" in path.relative_to(raw_root).parts:
                continue
            if path.name == "campaign-manifest.json":
                _check_campaign_manifest(path, errors, validators)
                continue
            try:
                payload = _read_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                errors.append(f"{path}: cannot read JSON: {error}")
                continue
            history_validator = validators.get("history.v1.json")
            if history_validator is not None:
                previous_error_count = len(errors)
                _check_schema_instance(payload, history_validator, str(path), errors)
                if len(errors) != previous_error_count:
                    continue
            try:
                history = read_history(path)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as error:
                errors.append(f"{path}: {error}")
                continue
            _check_manifest(history.manifest, f"{path}.manifest", errors)
            outcome_validator = validators.get("outcome.v1.json")
            if outcome_validator is not None:
                _check_schema_instance(
                    check_history(history).to_dict(),
                    outcome_validator,
                    f"{path}:outcome",
                    errors,
                )
            for index, operation in enumerate(history.operations):
                _require(bool(operation.operation_id), f"{path}: operation {index} has no operation_id", errors)
                capture_operations = history.manifest.get("topology_capture_operations", [])
                if (
                    isinstance(capture_operations, list)
                    and operation.operation_id in capture_operations
                ):
                    for field in ("topology_before", "topology_after"):
                        snapshot = getattr(operation, field)
                        _require(
                            isinstance(snapshot, dict),
                            f"{path}: RQ3 {operation.operation_id} is missing {field}",
                            errors,
                        )
                        if isinstance(snapshot, dict):
                            _require(
                                "term" in snapshot and isinstance(snapshot.get("members"), dict),
                                f"{path}: RQ3 {operation.operation_id} has incomplete {field}",
                                errors,
                            )
            for index, event in enumerate(history.fault_events):
                _require(isinstance(event, dict), f"{path}: fault event {index} must be an object", errors)
                if isinstance(event, dict):
                    event_validator = validators.get("fault-event.v1.json")
                    if event_validator is not None:
                        _check_schema_instance(
                            event,
                            event_validator,
                            f"{path}:fault_events[{index}]",
                            errors,
                        )

    summary = root / "results/summary/summary.json"
    if summary.is_file():
        _check_summary(summary, errors, validators)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = validate(args.root.resolve())
    if errors:
        print(f"Record validation failed: {len(errors)} finding(s)")
        for error in errors:
            print(error)
        return 1
    print("Record validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
