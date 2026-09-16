"""Typed records used by raw histories and offline checkers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    """Public classification for one checked history."""

    PASS = "PASS"
    VIOLATION = "VIOLATION"
    UNAVAILABLE = "UNAVAILABLE"
    INDETERMINATE = "INDETERMINATE"
    HARNESS_ERROR = "HARNESS_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CheckerResult:
    """One deterministic checker result."""

    outcome: Outcome
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


@dataclass
class OperationRecord:
    """One attempted database operation or observer snapshot."""

    operation_id: str
    kind: str
    key: str
    trial_id: str | None = None
    property: str | None = None
    operation_status: str = "SUCCESS"
    requested_member: str | None = None
    actual_server_address: str | None = None
    actual_role: str | None = None
    command_name: str | None = None
    session_id: str | None = None
    causal_session: bool | None = None
    read_concern: str | None = None
    write_concern: str | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    timeout_category: str | None = None
    fault_event_id: str | None = None
    version: int | None = None
    write_id: str | None = None
    observed_version: int | None = None
    observed_versions: tuple[int, ...] = ()
    observed_updates: tuple[dict[str, Any], ...] = ()
    parent_write_id: str | None = None
    depends_on_read_id: str | None = None
    depends_on_version: int | None = None
    dependency_metadata: dict[str, Any] = field(default_factory=dict)
    document_version_before: int | None = None
    document_version_after: int | None = None
    response_received: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observed_versions"] = list(self.observed_versions)
        payload["observed_updates"] = [dict(update) for update in self.observed_updates]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OperationRecord:
        values = dict(payload)
        values["observed_versions"] = tuple(values.get("observed_versions", ()))
        values["observed_updates"] = tuple(
            dict(update) for update in values.get("observed_updates", ())
        )
        return cls(**values)


@dataclass
class History:
    """A canonical trial history and its manifest."""

    manifest: dict[str, Any]
    operations: list[OperationRecord]
    fault_events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "history.v1"
    history_hash: str | None = None

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "manifest": dict(self.manifest),
            "operations": [operation.to_dict() for operation in self.operations],
            "fault_events": [dict(event) for event in self.fault_events],
            "metadata": dict(self.metadata),
        }
        if include_hash and self.history_hash is not None:
            payload["history_hash"] = self.history_hash
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> History:
        return cls(
            schema_version=str(payload.get("schema_version", "")),
            manifest=dict(payload.get("manifest", {})),
            operations=[
                OperationRecord.from_dict(operation)
                for operation in payload.get("operations", [])
            ],
            fault_events=[dict(event) for event in payload.get("fault_events", [])],
            metadata=dict(payload.get("metadata", {})),
            history_hash=payload.get("history_hash"),
        )
