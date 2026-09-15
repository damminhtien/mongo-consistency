"""Offline checkers for the four client-centric consistency properties."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .history import validate_history
from .models import CheckerResult, History, OperationRecord, Outcome


def _result(outcome: Outcome, reason: str, **details: Any) -> CheckerResult:
    return CheckerResult(outcome=outcome, reason=reason, details=details)


def _operations(history: History) -> dict[str, OperationRecord]:
    return {operation.operation_id: operation for operation in history.operations}


def _step_ids(history: History, defaults: Iterable[str]) -> list[str]:
    steps = history.manifest.get("property_steps", {})
    return [str(steps.get(name, name)) for name in defaults]


def _preflight(
    history: History, property_name: str, step_names: Iterable[str]
) -> tuple[dict[str, OperationRecord] | None, CheckerResult | None]:
    errors = validate_history(history)
    if errors:
        return None, _result(
            Outcome.HARNESS_ERROR,
            "history schema is invalid",
            errors=errors,
        )
    if history.manifest.get("cleanup_status") == "ERROR":
        return None, _result(
            Outcome.HARNESS_ERROR,
            "fault cleanup failed and the cluster state is not trusted",
            error=history.manifest.get("cleanup_error"),
        )
    if history.manifest.get("runner_error"):
        return None, _result(
            Outcome.HARNESS_ERROR,
            "runner recorded an execution error",
            error=history.manifest["runner_error"],
        )
    actual_property = history.manifest.get("property")
    if actual_property != property_name:
        return None, _result(
            Outcome.HARNESS_ERROR,
            "history property does not match checker",
            expected=property_name,
            actual=actual_property,
        )
    operations = _operations(history)
    names = list(step_names)
    ids = _step_ids(history, names)
    selected: dict[str, OperationRecord] = {}
    for name, operation_id in zip(names, ids, strict=True):
        operation = operations.get(operation_id)
        if operation is None:
            return None, _result(
                Outcome.HARNESS_ERROR,
                "required operation is missing",
                operation_id=operation_id,
                step=name,
            )
        selected[name] = operation
    for name, operation in selected.items():
        status = operation.operation_status
        if status == "UNSUPPORTED":
            return None, _result(
                Outcome.UNSUPPORTED,
                "required topology precondition was not established",
                operation_id=operation.operation_id,
                step=name,
            )
        if status == "HARNESS_ERROR":
            return None, _result(
                Outcome.HARNESS_ERROR,
                "runner reported a harness error",
                operation_id=operation.operation_id,
                step=name,
            )
        if status == "INDETERMINATE":
            return None, _result(
                Outcome.INDETERMINATE,
                "required operation has an ambiguous result",
                operation_id=operation.operation_id,
                step=name,
            )
        if status in {"TIMEOUT", "ERROR"}:
            if operation.kind == "write" or not operation.response_received:
                return None, _result(
                    Outcome.INDETERMINATE,
                    "a write may have completed before its response was lost",
                    operation_id=operation.operation_id,
                    step=name,
                )
            return None, _result(
                Outcome.UNAVAILABLE,
                "required read or observer operation did not complete",
                operation_id=operation.operation_id,
                step=name,
            )
        if status == "UNAVAILABLE":
            return None, _result(
                Outcome.UNAVAILABLE,
                "required operation was unavailable",
                operation_id=operation.operation_id,
                step=name,
            )
        if status != "SUCCESS":
            return None, _result(
                Outcome.HARNESS_ERROR,
                "operation status is not a supported successful state",
                operation_id=operation.operation_id,
                status=status,
            )
    return selected, None


def _same_key(operations: Iterable[OperationRecord]) -> CheckerResult | None:
    keys = {operation.key for operation in operations}
    if len(keys) != 1:
        return _result(
            Outcome.HARNESS_ERROR,
            "property fixture uses more than one logical key",
            keys=sorted(keys),
        )
    return None


def _read_version(operation: OperationRecord) -> int | None:
    if operation.observed_version is not None:
        return operation.observed_version
    if operation.observed_versions:
        return max(operation.observed_versions)
    return None


def _require_version(operation: OperationRecord, step: str) -> tuple[int | None, CheckerResult | None]:
    version = _read_version(operation)
    if version is None:
        return None, _result(
            Outcome.INDETERMINATE,
            "successful read has no observable logical version",
            operation_id=operation.operation_id,
            step=step,
        )
    return version, None


def _visible_write_ids(operation: OperationRecord) -> set[str]:
    return {
        str(update["write_id"])
        for update in operation.observed_updates
        if update.get("write_id")
    }


def _visible_versions(operation: OperationRecord) -> set[int]:
    versions = set(operation.observed_versions)
    versions.update(
        int(update["version"])
        for update in operation.observed_updates
        if isinstance(update.get("version"), int)
        and not isinstance(update.get("version"), bool)
    )
    return versions


def check_ryw(history: History) -> CheckerResult:
    """Check read-your-writes for write ``w1`` followed by read ``r1``."""

    selected, error = _preflight(history, "RYW", ("write", "read"))
    if error:
        return error
    assert selected is not None
    same_key_error = _same_key(selected.values())
    if same_key_error:
        return same_key_error
    write, read = selected["write"], selected["read"]
    if write.version is None:
        return _result(Outcome.HARNESS_ERROR, "write has no logical version")
    read_version, error = _require_version(read, "read")
    if error:
        return error
    assert read_version is not None
    if read_version < write.version:
        return _result(
            Outcome.VIOLATION,
            "read returned a version older than the preceding write",
            write_version=write.version,
            read_version=read_version,
        )
    return _result(
        Outcome.PASS,
        "read returned the written version or a later version",
        write_version=write.version,
        read_version=read_version,
    )


def check_mr(history: History) -> CheckerResult:
    """Check monotonic reads for successive reads ``r1`` and ``r2``."""

    selected, error = _preflight(history, "MR", ("first_read", "second_read"))
    if error:
        return error
    assert selected is not None
    same_key_error = _same_key(selected.values())
    if same_key_error:
        return same_key_error
    first, second = selected["first_read"], selected["second_read"]
    first_version, error = _require_version(first, "first_read")
    if error:
        return error
    second_version, error = _require_version(second, "second_read")
    if error:
        return error
    assert first_version is not None and second_version is not None
    if second_version < first_version:
        return _result(
            Outcome.VIOLATION,
            "second read returned a version older than the first read",
            first_version=first_version,
            second_version=second_version,
        )
    return _result(
        Outcome.PASS,
        "successive reads did not move to an older version",
        first_version=first_version,
        second_version=second_version,
    )


def check_mw(history: History) -> CheckerResult:
    """Check monotonic writes using one observer snapshot of the same key."""

    selected, error = _preflight(history, "MW", ("first_write", "second_write", "observer"))
    if error:
        return error
    assert selected is not None
    same_key_error = _same_key(selected.values())
    if same_key_error:
        return same_key_error
    first, second, observer = (
        selected["first_write"],
        selected["second_write"],
        selected["observer"],
    )
    if second.parent_write_id != first.write_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "second write does not name the first write as its parent",
            expected_parent=first.write_id,
            actual_parent=second.parent_write_id,
        )
    visible = _visible_write_ids(observer)
    if second.write_id not in visible:
        return _result(
            Outcome.INDETERMINATE,
            "observer snapshot does not show the completed successor write",
            visible_write_ids=sorted(visible),
            successor_write_id=second.write_id,
        )
    if first.write_id not in visible:
        return _result(
            Outcome.VIOLATION,
            "successor write is visible while its predecessor is absent",
            visible_write_ids=sorted(visible),
            predecessor_write_id=first.write_id,
            successor_write_id=second.write_id,
        )
    return _result(
        Outcome.PASS,
        "observer snapshot contains the predecessor before the successor",
        visible_write_ids=sorted(visible),
    )


def check_wfr(history: History) -> CheckerResult:
    """Check writes-follow-reads for read ``r1`` and dependent write ``w2``."""

    selected, error = _preflight(history, "WFR", ("read", "write", "observer"))
    if error:
        return error
    assert selected is not None
    same_key_error = _same_key(selected.values())
    if same_key_error:
        return same_key_error
    read, write, observer = selected["read"], selected["write"], selected["observer"]
    read_version, error = _require_version(read, "read")
    if error:
        return error
    assert read_version is not None
    if write.depends_on_read_id != read.operation_id:
        return _result(
            Outcome.HARNESS_ERROR,
            "dependent write does not name the preceding read",
            expected_read_id=read.operation_id,
            actual_read_id=write.depends_on_read_id,
        )
    if write.depends_on_version != read_version:
        return _result(
            Outcome.HARNESS_ERROR,
            "dependent write does not name the version returned by the read",
            expected_version=read_version,
            actual_version=write.depends_on_version,
        )
    visible = _visible_write_ids(observer)
    if write.write_id not in visible:
        return _result(
            Outcome.INDETERMINATE,
            "observer snapshot does not show the dependent write",
            visible_write_ids=sorted(visible),
            dependent_write_id=write.write_id,
        )
    visible_versions = _visible_versions(observer)
    if read_version not in visible_versions:
        return _result(
            Outcome.VIOLATION,
            "dependent write is visible while the version it read is absent",
            visible_versions=sorted(visible_versions),
            read_version=read_version,
            dependent_write_id=write.write_id,
        )
    return _result(
        Outcome.PASS,
        "observer snapshot contains the read dependency and dependent write",
        visible_versions=sorted(visible_versions),
    )


CHECKERS = {
    "RYW": check_ryw,
    "MR": check_mr,
    "MW": check_mw,
    "WFR": check_wfr,
}


def check_history(history: History, property_name: str | None = None) -> CheckerResult:
    """Run the checker selected by the manifest or explicit property name."""

    selected_property = property_name or str(history.manifest.get("property", ""))
    checker = CHECKERS.get(selected_property)
    if checker is None:
        return _result(
            Outcome.HARNESS_ERROR,
            "no checker is registered for the property",
            property=selected_property,
        )
    return checker(history)
