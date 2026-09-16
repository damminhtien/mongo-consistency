"""Validate committed record formats and any generated experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mongo_consistency.config import load_configurations, load_json, load_predictions
from mongo_consistency.history import read_history
from mongo_consistency.models import Outcome

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAMES = (
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
    _require(manifest.get("campaign_id") in {"pilot", "normal", "experiment"}, f"{location}: invalid campaign_id", errors)
    _require(manifest.get("configuration_id") in {f"C{number}" for number in range(1, 9)}, f"{location}: invalid configuration_id", errors)
    _require(manifest.get("property") in {"RYW", "MR", "MW", "WFR"}, f"{location}: invalid property", errors)
    _require(isinstance(manifest.get("causal_session"), bool), f"{location}: causal_session must be boolean", errors)
    _require(isinstance(manifest.get("namespace"), dict), f"{location}: namespace must be an object", errors)
    _require(isinstance(manifest.get("timeout_policy"), dict), f"{location}: timeout_policy must be an object", errors)


def _check_campaign_manifest(path: Path, errors: list[str]) -> None:
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return
    _require(isinstance(payload, dict), f"{path}: expected an object", errors)
    if not isinstance(payload, dict):
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
        global_count = payload.get("global_expected_case_count")
        if global_count is not None:
            if isinstance(expected_count, int) and not isinstance(expected_count, bool):
                _require(
                    isinstance(global_count, int)
                    and not isinstance(global_count, bool)
                    and global_count >= expected_count,
                    f"{path}: invalid global_expected_case_count",
                    errors,
                )
            else:
                _require(False, f"{path}: cannot check global_expected_case_count", errors)
        shard_count = payload.get("shard_count")
        if shard_count is not None:
            _require(
                isinstance(shard_count, int) and not isinstance(shard_count, bool) and shard_count >= 1,
                f"{path}: invalid shard_count",
                errors,
            )


def _check_summary(path: Path, errors: list[str]) -> None:
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"{path}: cannot read JSON: {error}")
        return
    _require(isinstance(payload, dict), f"{path}: expected an object", errors)
    if not isinstance(payload, dict):
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
    for schema_name in SCHEMA_NAMES:
        path = root / "schemas" / schema_name
        try:
            payload = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{path}: cannot read schema: {error}")
            continue
        _require(isinstance(payload, dict), f"{path}: schema must be an object", errors)

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
                _check_campaign_manifest(path, errors)
                continue
            try:
                history = read_history(path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                errors.append(f"{path}: {error}")
                continue
            _check_manifest(history.manifest, f"{path}.manifest", errors)
            for index, operation in enumerate(history.operations):
                _require(bool(operation.operation_id), f"{path}: operation {index} has no operation_id", errors)
            for index, event in enumerate(history.fault_events):
                _require(isinstance(event, dict), f"{path}: fault event {index} must be an object", errors)

    summary = root / "results/summary/summary.json"
    if summary.is_file():
        _check_summary(summary, errors)
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
