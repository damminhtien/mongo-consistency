"""Canonical serialization, validation, and hashing for trial histories."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .models import History

SCHEMA_VERSION = "history.v1"
VALID_STATUSES = frozenset(
    {
        "SUCCESS",
        "UNAVAILABLE",
        "INDETERMINATE",
        "HARNESS_ERROR",
    }
)


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a payload without whitespace or platform-specific ordering."""

    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_payload(history: History | dict[str, Any]) -> dict[str, Any]:
    """Return the hash input with a stored hash removed."""

    payload = history.to_dict(include_hash=False) if isinstance(history, History) else dict(history)
    payload.pop("history_hash", None)
    return payload


def compute_history_hash(history: History | dict[str, Any]) -> str:
    """Compute the SHA-256 hash of a canonical history payload."""

    return hashlib.sha256(canonical_bytes(canonical_payload(history))).hexdigest()


def validate_history(history: History | dict[str, Any]) -> list[str]:
    """Return deterministic validation errors for a raw history."""

    value = History.from_dict(history) if isinstance(history, dict) else history
    errors: list[str] = []
    if value.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {value.schema_version!r}")
    if not isinstance(value.manifest, dict) or not value.manifest.get("trial_id"):
        errors.append("manifest.trial_id is required")
    if not isinstance(value.precondition, dict):
        errors.append("precondition must be an object")
    else:
        status = value.precondition.get("status")
        if status not in {"SATISFIED", "PRECONDITION_MISS"}:
            errors.append("precondition.status must be SATISFIED or PRECONDITION_MISS")
        checks = value.precondition.get("checks")
        if not isinstance(checks, list):
            errors.append("precondition.checks must be a list")
        elif not checks:
            errors.append("precondition.checks must contain at least one observed check")
        else:
            check_statuses = []
            for index, check in enumerate(checks):
                if not isinstance(check, dict):
                    errors.append(f"precondition.checks[{index}] must be an object")
                    continue
                if not isinstance(check.get("name"), str) or not check["name"]:
                    errors.append(f"precondition.checks[{index}].name is required")
                check_status = check.get("status")
                if check_status not in {"SATISFIED", "PRECONDITION_MISS"}:
                    errors.append(
                        f"precondition.checks[{index}].status must be SATISFIED or PRECONDITION_MISS"
                    )
                check_statuses.append(check_status)
            if status == "SATISFIED" and any(
                check_status != "SATISFIED" for check_status in check_statuses
            ):
                errors.append("SATISFIED precondition contains a failed check")
            if status == "PRECONDITION_MISS" and "PRECONDITION_MISS" not in check_statuses:
                errors.append("PRECONDITION_MISS precondition has no failed check")
    if not isinstance(value.diagnostics, list) or any(
        not isinstance(event, dict) for event in value.diagnostics
    ):
        errors.append("diagnostics must be a list of objects")
    if value.final_observation is not None and not isinstance(value.final_observation, dict):
        errors.append("final_observation must be an object or null")
    operation_ids: set[str] = set()
    for index, operation in enumerate(value.operations):
        if not isinstance(operation.operation_id, str) or not operation.operation_id:
            errors.append(f"operations[{index}].operation_id is required")
        elif operation.operation_id in operation_ids:
            errors.append(f"duplicate operation_id: {operation.operation_id}")
        operation_ids.add(operation.operation_id)
        if operation.kind not in {"read", "write"}:
            errors.append(f"operations[{index}].kind is invalid: {operation.kind!r}")
        if not operation.key:
            errors.append(f"operations[{index}].key is required")
        if operation.operation_status not in VALID_STATUSES:
            errors.append(
                f"operations[{index}].operation_status is invalid: "
                f"{operation.operation_status!r}"
            )
        if not isinstance(operation.command_started, bool):
            errors.append(f"operations[{index}].command_started must be boolean")
        if not isinstance(operation.response_received, bool):
            errors.append(f"operations[{index}].response_received must be boolean")
        if operation.observed_document_exists is not None and not isinstance(
            operation.observed_document_exists, bool
        ):
            errors.append(f"operations[{index}].observed_document_exists must be boolean or null")
        if operation.kind == "write":
            if not isinstance(operation.write_id, str) or not operation.write_id:
                errors.append(f"operations[{index}].write_id is required for writes")
            if operation.intended_version is None:
                errors.append(f"operations[{index}].intended_version is required for writes")
        if operation.kind != "read" and (
            operation.observed_document_exists is not None
            or operation.observed_version is not None
            or operation.observed_versions
            or operation.observed_write_ids
            or operation.observed_updates
        ):
            errors.append(f"operations[{index}] has observed_* fields on a non-read operation")
        if operation.operation_status != "SUCCESS" and (
            operation.observed_document_exists is not None
            or operation.observed_version is not None
            or operation.observed_versions
            or operation.observed_write_ids
            or operation.observed_updates
        ):
            errors.append(f"operations[{index}] has observations without a successful response")
        for field_name, version in (
            ("intended_version", operation.intended_version),
            ("observed_version", operation.observed_version),
            ("depends_on_version", operation.depends_on_version),
        ):
            if version is not None and (
                isinstance(version, bool) or not isinstance(version, int) or version < 0
            ):
                errors.append(f"operations[{index}].{field_name} must be a non-negative integer")
        for update_index, update in enumerate(operation.observed_updates):
            if not isinstance(update, dict) or not update.get("write_id"):
                errors.append(
                    f"operations[{index}].observed_updates[{update_index}] "
                    "needs write_id"
                )
            version = update.get("version") if isinstance(update, dict) else None
            if version is not None and (
                isinstance(version, bool) or not isinstance(version, int) or version < 0
            ):
                errors.append(
                    f"operations[{index}].observed_updates[{update_index}].version "
                    "must be a non-negative integer"
                )
        for field_name, values in (
            ("observed_versions", operation.observed_versions),
            ("observed_write_ids", operation.observed_write_ids),
        ):
            if not isinstance(values, tuple):
                errors.append(f"operations[{index}].{field_name} must be a tuple")
        if any(
            isinstance(version, bool) or not isinstance(version, int) or version < 0
            for version in operation.observed_versions
        ):
            errors.append(f"operations[{index}].observed_versions must contain non-negative integers")
        if any(not isinstance(write_id, str) or not write_id for write_id in operation.observed_write_ids):
            errors.append(f"operations[{index}].observed_write_ids must contain non-empty strings")
        if operation.observed_versions and operation.observed_version is not None:
            if operation.observed_version != max(operation.observed_versions):
                errors.append(
                    f"operations[{index}].observed_version does not match observed_versions"
                )
        if not isinstance(operation.command_events, tuple) or any(
            not isinstance(event, dict) for event in operation.command_events
        ):
            errors.append(f"operations[{index}].command_events must be a tuple of objects")
        for time_field in (
            "cluster_time_before",
            "operation_time_before",
            "cluster_time_after",
            "operation_time_after",
            "after_cluster_time",
        ):
            time_value = getattr(operation, time_field)
            if time_value is not None and not isinstance(time_value, dict):
                errors.append(f"operations[{index}].{time_field} must be an object or null")
    stored_hash = value.history_hash
    if stored_hash and stored_hash != compute_history_hash(value):
        errors.append("history_hash does not match canonical history")
    return errors


def read_history(path: Path) -> History:
    """Read and validate one JSON history."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    history = History.from_dict(payload)
    errors = validate_history(history)
    if errors:
        raise ValueError(f"Invalid history {path}: {'; '.join(errors)}")
    return history


def write_history(path: Path, history: History) -> str:
    """Write a canonical JSON history and return its hash."""

    errors = validate_history(history)
    if errors:
        raise ValueError(f"Cannot write invalid history: {'; '.join(errors)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    history.history_hash = compute_history_hash(history)
    payload = (
        json.dumps(
            history.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_bytes(payload)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return history.history_hash
