"""Canonical serialization, validation, and hashing for trial histories."""

from __future__ import annotations

import hashlib
import json
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
        "UNSUPPORTED",
        "ERROR",
        "TIMEOUT",
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
    operation_ids: set[str] = set()
    for index, operation in enumerate(value.operations):
        if not operation.operation_id:
            errors.append(f"operations[{index}].operation_id is required")
        elif operation.operation_id in operation_ids:
            errors.append(f"duplicate operation_id: {operation.operation_id}")
        operation_ids.add(operation.operation_id)
        if operation.kind not in {"read", "write", "observer", "setup"}:
            errors.append(f"operations[{index}].kind is invalid: {operation.kind!r}")
        if not operation.key:
            errors.append(f"operations[{index}].key is required")
        if operation.operation_status not in VALID_STATUSES:
            errors.append(
                f"operations[{index}].operation_status is invalid: "
                f"{operation.operation_status!r}"
            )
        for field_name, version in (
            ("version", operation.version),
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

    path.parent.mkdir(parents=True, exist_ok=True)
    history.history_hash = compute_history_hash(history)
    path.write_bytes(
        json.dumps(
            history.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    return history.history_hash
