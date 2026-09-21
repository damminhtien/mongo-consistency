"""Typed records used by raw histories and offline checkers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    """Final classification for one checked history."""

    PASS = "PASS"
    VIOLATION = "VIOLATION"
    UNAVAILABLE = "UNAVAILABLE"
    INDETERMINATE = "INDETERMINATE"
    PRECONDITION_MISS = "PRECONDITION_MISS"
    HARNESS_ERROR = "HARNESS_ERROR"


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
    """One operation issued by the subject client."""

    operation_id: str
    kind: str
    key: str
    trial_id: str | None = None
    property: str | None = None
    operation_status: str = "SUCCESS"
    requested_member: str | None = None
    actual_server_address: str | None = None
    actual_role: str | None = None
    actual_role_observed_at_ns: int | None = None
    driver_reported_role: str | None = None
    command_name: str | None = None
    command_started: bool = False
    command_events: tuple[dict[str, Any], ...] = ()
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
    fault_state: dict[str, Any] = field(default_factory=dict)
    intended_version: int | None = None
    write_id: str | None = None
    observed_version: int | None = None
    observed_document_exists: bool | None = None
    observed_versions: tuple[int, ...] = ()
    observed_write_ids: tuple[str, ...] = ()
    observed_updates: tuple[dict[str, Any], ...] = ()
    parent_write_id: str | None = None
    depends_on_read_id: str | None = None
    depends_on_version: int | None = None
    dependency_metadata: dict[str, Any] = field(default_factory=dict)
    cluster_time_before: dict[str, Any] | None = None
    operation_time_before: dict[str, Any] | None = None
    cluster_time_after: dict[str, Any] | None = None
    operation_time_after: dict[str, Any] | None = None
    after_cluster_time: dict[str, Any] | None = None
    topology_before: dict[str, Any] | None = None
    topology_after: dict[str, Any] | None = None
    response_received: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["command_events"] = [dict(event) for event in self.command_events]
        payload["observed_versions"] = list(self.observed_versions)
        payload["observed_write_ids"] = list(self.observed_write_ids)
        payload["observed_updates"] = [dict(update) for update in self.observed_updates]
        if self.topology_before is None:
            payload.pop("topology_before")
        if self.topology_after is None:
            payload.pop("topology_after")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OperationRecord:
        values = dict(payload)
        values["command_events"] = tuple(
            dict(event) for event in values.get("command_events", ())
        )
        values["observed_versions"] = tuple(values.get("observed_versions", ()))
        values["observed_write_ids"] = tuple(values.get("observed_write_ids", ()))
        values["observed_updates"] = tuple(
            dict(update) for update in values.get("observed_updates", ())
        )
        return cls(**values)


@dataclass
class History:
    """One logical history plus independent setup and diagnostic evidence."""

    manifest: dict[str, Any]
    operations: list[OperationRecord]
    precondition: dict[str, Any] = field(
        default_factory=lambda: {"status": "PRECONDITION_MISS", "checks": []}
    )
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    final_observation: dict[str, Any] | None = None
    fault_events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "history.v1"
    history_hash: str | None = None

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "manifest": dict(self.manifest),
            "precondition": dict(self.precondition),
            "operations": [operation.to_dict() for operation in self.operations],
            "diagnostics": [dict(event) for event in self.diagnostics],
            "final_observation": (
                dict(self.final_observation) if self.final_observation is not None else None
            ),
            "fault_events": [dict(event) for event in self.fault_events],
            "metadata": dict(self.metadata),
        }
        if include_hash and self.history_hash is not None:
            payload["history_hash"] = self.history_hash
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> History:
        observation = payload.get("final_observation")
        return cls(
            schema_version=str(payload.get("schema_version", "")),
            manifest=dict(payload.get("manifest", {})),
            precondition=dict(payload.get("precondition", {})),
            operations=[
                OperationRecord.from_dict(operation)
                for operation in payload.get("operations", [])
            ],
            diagnostics=[dict(event) for event in payload.get("diagnostics", [])],
            final_observation=dict(observation) if isinstance(observation, dict) else None,
            fault_events=[dict(event) for event in payload.get("fault_events", [])],
            metadata=dict(payload.get("metadata", {})),
            history_hash=payload.get("history_hash"),
        )
