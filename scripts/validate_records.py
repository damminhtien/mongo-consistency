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
    "manifest.v1.json",
    "operation.v1.json",
    "fault-event.v1.json",
    "history.v1.json",
    "outcome.v1.json",
    "summary.v1.json",
)


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
    _require(manifest.get("campaign_id") in {"smoke", "pilot", "normal", "experiment", "rq2"}, f"{location}: invalid campaign_id", errors)
    _require(manifest.get("configuration_id") in {f"C{number}" for number in range(1, 9)}, f"{location}: invalid configuration_id", errors)
    _require(manifest.get("property") in {"RYW", "MR", "MW", "WFR"}, f"{location}: invalid property", errors)
    _require(isinstance(manifest.get("causal_session"), bool), f"{location}: causal_session must be boolean", errors)
    _require(isinstance(manifest.get("namespace"), dict), f"{location}: namespace must be an object", errors)
    _require(isinstance(manifest.get("timeout_policy"), dict), f"{location}: timeout_policy must be an object", errors)


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
            for index, event in enumerate(history.fault_events):
                _require(isinstance(event, dict), f"{path}: fault event {index} must be an object", errors)

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
