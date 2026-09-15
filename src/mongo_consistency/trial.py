"""One live MongoDB trial with one explicit PyMongo session."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .driver import (
    ClientSettings,
    RoutingMonitor,
    address_text,
    collection_for,
    create_client,
    operation_record_from_events,
    tagged_secondary,
)
from .models import History, OperationRecord


NETWORK_ERRORS = frozenset(
    {
        "AutoReconnect",
        "ConnectionFailure",
        "ExecutionTimeout",
        "NetworkTimeout",
        "ServerSelectionTimeoutError",
        "WriteConcernError",
    }
)


def classify_exception(error: Exception, kind: str) -> tuple[str, str | None]:
    """Map a PyMongo exception to a conservative raw-history status."""

    name = type(error).__name__
    if name in NETWORK_ERRORS or "Timeout" in name or "Connection" in name:
        return ("INDETERMINATE" if kind == "write" else "UNAVAILABLE", name)
    return "HARNESS_ERROR", name


class MongoTrial:
    """Issue operations and retain the complete trace in memory until write-out."""

    def __init__(
        self,
        *,
        seed_uris: tuple[str, ...],
        configuration: dict[str, Any],
        trial_id: str,
        property_name: str,
        schedule_id: str,
        seed: int,
    ) -> None:
        self.configuration = configuration
        self.trial_id = trial_id
        self.property_name = property_name
        self.schedule_id = schedule_id
        self.seed = seed
        self.database_name = f"mc_{trial_id.replace('-', '_')}"
        self.collection_name = "logical"
        self.document_id = f"{trial_id}/x"
        self.roles: dict[str, str | None] = {}
        self.monitor = RoutingMonitor(lambda address: self.roles.get(address))
        self.client = create_client(
            ClientSettings(seed_uris=seed_uris),
            monitor=self.monitor,
        )
        self.session: Any | None = None
        self.operations: list[OperationRecord] = []
        self.fault_events: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {
            "schema_version": "manifest.v1",
            "trial_id": trial_id,
            "campaign_id": "unassigned",
            "configuration_id": configuration["id"],
            "read_concern": configuration["read_concern"],
            "write_concern": configuration["write_concern"],
            "causal_session": configuration["causal_session"],
            "property": property_name,
            "schedule_id": schedule_id,
            "seed": seed,
            "namespace": {
                "database": self.database_name,
                "collection": self.collection_name,
                "document_id": self.document_id,
            },
            "retry_reads": False,
            "retry_writes": False,
            "timeout_policy": {
                "connect_ms": 2000,
                "server_selection_ms": 5000,
                "operation_ms": 5000,
                "write_concern_ms": 5000,
                "election_barrier_ms": 30000,
                "subtrial_ms": 60000,
            },
        }

    def __enter__(self) -> "MongoTrial":
        self.client.admin.command("ping")
        self.refresh_roles()
        pymongo = __import__("pymongo")
        self.session = self.client.start_session(
            causal_consistency=bool(self.configuration["causal_session"])
        )
        self.manifest["session_id"] = self.session_id
        self.manifest["driver_version"] = pymongo.version
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    @property
    def session_id(self) -> str:
        if self.session is None:
            return "unstarted"
        return repr(self.session.session_id.get("id"))

    def close(self) -> None:
        if self.session is not None:
            self.session.end_session()
            self.session = None
        self.client.close()

    def refresh_roles(self) -> dict[str, str | None]:
        descriptions = self.client.topology_description.server_descriptions()
        self.roles = {
            address_text(address): getattr(description, "server_type_name", None)
            for address, description in descriptions.items()
        }
        self.manifest["members"] = [
            {"address": address, "actual_role": role}
            for address, role in sorted(self.roles.items())
        ]
        return dict(self.roles)

    def primary_member(self) -> str | None:
        primary = self.client.primary
        if primary is None:
            self.refresh_roles()
            primary = self.client.primary
        if primary is None:
            return None
        value = address_text(primary)
        return value.split(":", 1)[0]

    def set_campaign(self, campaign_id: str) -> None:
        self.manifest["campaign_id"] = campaign_id

    def set_steps(self, steps: dict[str, str]) -> None:
        self.manifest["property_steps"] = dict(steps)

    def add_fault_event(self, event: dict[str, Any]) -> None:
        self.fault_events.append(dict(event))

    def _collection(self, requested_member: str | None = None) -> Any:
        preference = tagged_secondary(requested_member) if requested_member else None
        return collection_for(
            self.client,
            self.database_name,
            self.collection_name,
            read_concern=self.configuration["read_concern"],
            write_concern=self.configuration["write_concern"],
            read_preference=preference,
        )

    def _execute(
        self,
        operation: OperationRecord,
        action: Callable[[], Any],
    ) -> tuple[OperationRecord, Any | None]:
        if self.session is None:
            raise RuntimeError("trial session has not started")
        wrapper_start = time.monotonic_ns()
        result: Any | None = None
        error: Exception | None = None
        self.monitor.attach(operation.operation_id)
        try:
            result = action()
            operation.operation_status = "SUCCESS"
        except Exception as caught:  # PyMongo exposes many error subclasses.
            error = caught
            operation.operation_status, operation.error_code = classify_exception(
                caught, operation.kind
            )
            operation.error_message = str(caught)
            operation.response_received = False
        finally:
            self.monitor.detach()
        wrapper_end = time.monotonic_ns()
        operation.start_ns = wrapper_start
        operation.end_ns = wrapper_end
        operation_record_from_events(
            operation,
            self.monitor.events_for(operation.operation_id),
        )
        if error is not None:
            operation.error_message = str(error)
            operation.response_received = False
        self.operations.append(operation)
        return operation, result

    def initialize(self) -> OperationRecord:
        """Create the logical document and its version-zero update."""

        operation = OperationRecord(
            operation_id="init",
            kind="setup",
            key="x",
            session_id=self.session_id,
            causal_session=bool(self.configuration["causal_session"]),
            write_concern=self.configuration["write_concern"],
            requested_member="primary",
        )

        def action() -> Any:
            return self._collection().replace_one(
                {"_id": self.document_id},
                {
                    "_id": self.document_id,
                    "updates": [
                        {
                            "write_id": "init",
                            "version": 0,
                            "effect": "initial",
                            "parent_write_id": None,
                            "depends_on_read_id": None,
                            "depends_on_version": None,
                        }
                    ],
                },
                upsert=True,
                session=self.session,
            )

        operation, _ = self._execute(operation, action)
        return operation

    def write(
        self,
        operation_id: str,
        *,
        write_id: str,
        version: int,
        parent_write_id: str | None = None,
        depends_on_read_id: str | None = None,
        depends_on_version: int | None = None,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        update = {
            "write_id": write_id,
            "version": version,
            "effect": f"set-v{version}",
            "parent_write_id": parent_write_id,
            "depends_on_read_id": depends_on_read_id,
            "depends_on_version": depends_on_version,
        }
        operation = OperationRecord(
            operation_id=operation_id,
            kind="write",
            key="x",
            session_id=self.session_id,
            causal_session=bool(self.configuration["causal_session"]),
            write_concern=self.configuration["write_concern"],
            requested_member="primary",
            version=version,
            write_id=write_id,
            parent_write_id=parent_write_id,
            depends_on_read_id=depends_on_read_id,
            depends_on_version=depends_on_version,
            dependency_metadata={
                "parent_write_id": parent_write_id,
                "depends_on_read_id": depends_on_read_id,
                "depends_on_version": depends_on_version,
            },
            fault_event_id=fault_event_id,
        )

        def action() -> Any:
            result = self._collection().update_one(
                {"_id": self.document_id},
                {"$push": {"updates": update}},
                upsert=False,
                session=self.session,
            )
            if result.matched_count != 1:
                raise RuntimeError("logical document was not found")
            return result

        return self._execute(operation, action)[0]

    def read(
        self,
        operation_id: str,
        *,
        requested_member: str | None = None,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        return self._read_like(
            operation_id,
            kind="read",
            requested_member=requested_member,
            fault_event_id=fault_event_id,
        )

    def observe(
        self,
        operation_id: str,
        *,
        requested_member: str | None = None,
        fault_event_id: str | None = None,
    ) -> OperationRecord:
        return self._read_like(
            operation_id,
            kind="observer",
            requested_member=requested_member,
            fault_event_id=fault_event_id,
        )

    def _read_like(
        self,
        operation_id: str,
        *,
        kind: str,
        requested_member: str | None,
        fault_event_id: str | None,
    ) -> OperationRecord:
        operation = OperationRecord(
            operation_id=operation_id,
            kind=kind,
            key="x",
            session_id=self.session_id,
            causal_session=bool(self.configuration["causal_session"]),
            read_concern=self.configuration["read_concern"],
            write_concern=self.configuration["write_concern"],
            requested_member=requested_member or "primary",
            fault_event_id=fault_event_id,
        )

        def action() -> Any:
            document = self._collection(requested_member).find_one(
                {"_id": self.document_id},
                session=self.session,
            )
            if not document or not isinstance(document.get("updates"), list):
                raise RuntimeError("logical document or update list is missing")
            operation.observed_updates = tuple(
                dict(update) for update in document["updates"]
            )
            versions = sorted(
                update["version"]
                for update in operation.observed_updates
                if isinstance(update.get("version"), int)
                and not isinstance(update.get("version"), bool)
            )
            operation.observed_versions = tuple(versions)
            operation.observed_version = versions[-1] if versions else None
            return document

        return self._execute(operation, action)[0]

    def history(self) -> History:
        self.refresh_roles()
        self.manifest["session_id"] = self.session_id
        return History(
            manifest=dict(self.manifest),
            operations=list(self.operations),
            fault_events=list(self.fault_events),
            metadata=dict(self.metadata),
        )
