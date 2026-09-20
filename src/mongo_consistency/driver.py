"""PyMongo construction and command-routing telemetry.

The module keeps PyMongo imports lazy so offline history checking remains
usable on machines that only have the Python standard library installed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .models import OperationRecord


class PyMongoUnavailable(RuntimeError):
    """Raised when a live MongoDB operation lacks the pinned driver."""


def require_pymongo() -> Any:
    """Import PyMongo only for commands that need a live database."""

    try:
        import pymongo
    except ImportError as error:
        raise PyMongoUnavailable(
            "PyMongo 4.18.1 is required for live trials; install requirements.txt"
        ) from error
    expected = "4.18.1"
    if pymongo.version != expected:
        raise PyMongoUnavailable(
            f"PyMongo {expected} is required; found {pymongo.version}"
        )
    return pymongo


def address_text(address: Any) -> str:
    """Render a PyMongo connection address without losing its port."""

    if isinstance(address, tuple) and len(address) == 2:
        return f"{address[0]}:{address[1]}"
    return str(address)


def _timestamp(value: Any) -> dict[str, int] | None:
    """Convert BSON timestamps to stable JSON values without serializing BSON types."""

    seconds = getattr(value, "time", None)
    increment = getattr(value, "inc", None)
    if isinstance(seconds, int) and isinstance(increment, int):
        return {"seconds": seconds, "increment": increment}
    if isinstance(value, dict):
        seconds = value.get("t")
        increment = value.get("i")
        if isinstance(seconds, int) and isinstance(increment, int):
            return {"seconds": seconds, "increment": increment}
    return None


def _cluster_timestamp(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return _timestamp(value.get("clusterTime"))


def _concern(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "afterClusterTime":
            converted = _timestamp(item)
            if converted is not None:
                result[key] = converted
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[str(key)] = item
    return result


def session_cluster_time(session: Any) -> dict[str, Any] | None:
    """Return a compact copy of the session cluster-time document."""

    value = getattr(session, "cluster_time", None)
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    timestamp = _timestamp(value.get("clusterTime"))
    if timestamp is not None:
        result["cluster_time"] = timestamp
    signature = value.get("signature")
    if isinstance(signature, dict) and isinstance(signature.get("keyId"), int):
        result["signature_key_id"] = signature["keyId"]
    return result or None


def session_operation_time(session: Any) -> dict[str, int] | None:
    return _timestamp(getattr(session, "operation_time", None))


class RoutingMonitor:
    """Command listener that ties actual server routing to one operation."""

    def __init__(self, role_lookup: Callable[[str], str | None] | None = None) -> None:
        self._lock = Lock()
        self._current_operation: str | None = None
        self._pending: dict[int, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._role_lookup = role_lookup or (lambda _address: None)

    def attach(self, operation_id: str) -> None:
        with self._lock:
            self._current_operation = operation_id

    def detach(self) -> None:
        with self._lock:
            self._current_operation = None

    def _record(self, operation_id: str | None, event: dict[str, Any]) -> None:
        if operation_id is None:
            return
        self._events.setdefault(operation_id, []).append(event)

    def started(self, event: Any) -> None:
        command_name = str(getattr(event, "command_name", ""))
        if command_name in {"hello", "ismaster", "isMaster"}:
            return
        with self._lock:
            operation_id = self._current_operation
            command = getattr(event, "command", {})
            command = command if isinstance(command, dict) else {}
            read_concern = command.get("readConcern")
            write_concern = command.get("writeConcern")
            entry = {
                "operation_id": operation_id,
                "request_id": int(event.request_id),
                "command_name": command_name,
                "started_ns": time.monotonic_ns(),
                "actual_server_address": address_text(event.connection_id),
                "driver_reported_role": self._role_lookup(address_text(event.connection_id)),
                "command_started": True,
                "status": "STARTED",
                "cluster_time_sent": _cluster_timestamp(command.get("$clusterTime")),
                "operation_time_sent": _timestamp(command.get("operationTime")),
                "read_concern": _concern(read_concern),
                "write_concern": _concern(write_concern),
                "after_cluster_time": (
                    _timestamp(read_concern.get("afterClusterTime"))
                    if isinstance(read_concern, dict)
                    else None
                ),
            }
            self._pending[int(event.request_id)] = entry
            if operation_id is not None:
                self._events.setdefault(operation_id, []).append(entry)

    def succeeded(self, event: Any) -> None:
        self._finish(event, "SUCCESS", None)

    def failed(self, event: Any) -> None:
        failure = getattr(event, "failure", None)
        self._finish(event, "ERROR", str(failure) if failure else None)

    def _finish(self, event: Any, status: str, error_message: str | None) -> None:
        with self._lock:
            pending = self._pending.pop(int(event.request_id), None)
            if pending is None:
                return
            reply = getattr(event, "reply", None)
            reply = reply if isinstance(reply, dict) else {}
            pending.update(
                {
                    "status": status,
                    "end_ns": time.monotonic_ns(),
                    "cluster_time_received": _cluster_timestamp(reply.get("$clusterTime")),
                    "operation_time_received": _timestamp(reply.get("operationTime")),
                    "error_message": error_message,
                }
            )

    def events_for(self, operation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events.pop(operation_id, [])]

    def command_listener(self, listener_type: Any) -> Any:
        """Build a PyMongo listener adapter without importing PyMongo offline."""

        monitor = self

        class Listener(listener_type):
            def started(self, event: Any) -> None:
                monitor.started(event)

            def succeeded(self, event: Any) -> None:
                monitor.succeeded(event)

            def failed(self, event: Any) -> None:
                monitor.failed(event)

        return Listener()


@dataclass(frozen=True)
class ClientSettings:
    """Pinned connection settings shared by all live trial clients."""

    seed_uris: tuple[str, ...]
    replica_set: str = "rs0"
    connect_timeout_ms: int = 2000
    server_selection_timeout_ms: int = 5000
    socket_timeout_ms: int = 5000
    retry_reads: bool = False
    retry_writes: bool = False


def create_client(settings: ClientSettings, monitor: RoutingMonitor | None = None) -> Any:
    """Create a PyMongo client with retries disabled and bounded deadlines."""

    pymongo = require_pymongo()
    options: dict[str, Any] = {
        "replicaSet": settings.replica_set,
        "connectTimeoutMS": settings.connect_timeout_ms,
        "serverSelectionTimeoutMS": settings.server_selection_timeout_ms,
        "socketTimeoutMS": settings.socket_timeout_ms,
        "retryReads": settings.retry_reads,
        "retryWrites": settings.retry_writes,
    }
    if monitor is not None:
        options["event_listeners"] = [
            monitor.command_listener(pymongo.monitoring.CommandListener)
        ]
    return pymongo.MongoClient(list(settings.seed_uris), **options)


def collection_for(
    client: Any,
    database_name: str,
    collection_name: str,
    *,
    read_concern: str,
    write_concern: str,
    read_preference: Any | None = None,
) -> Any:
    """Create a collection with the trial's concerns and optional routing."""

    pymongo = require_pymongo()
    database = client.get_database(
        database_name,
        read_concern=pymongo.read_concern.ReadConcern(level=read_concern),
        write_concern=pymongo.write_concern.WriteConcern(
            w=1 if write_concern == "w:1" else "majority",
            wtimeout=5000,
        ),
    )
    collection = database.get_collection(collection_name)
    if read_preference is not None:
        collection = collection.with_options(read_preference=read_preference)
    return collection


def tagged_secondary(member_tag: str) -> Any:
    """Return a secondary read preference for a Compose member tag."""

    pymongo = require_pymongo()
    return pymongo.read_preferences.Secondary(
        tag_sets=[{"member": member_tag}]
    )


def operation_record_from_events(
    operation: OperationRecord,
    events: list[dict[str, Any]],
) -> OperationRecord:
    """Copy command-monitor routing and timing into an operation record."""

    if not events:
        return operation
    operation.command_events = tuple(dict(event) for event in events)
    operation.command_started = any(event.get("command_started") for event in events)
    event = next(
        (item for item in reversed(events) if item.get("actual_server_address")),
        events[-1],
    )
    operation.actual_server_address = event.get("actual_server_address")
    operation.driver_reported_role = event.get("driver_reported_role")
    operation.command_name = event.get("command_name")
    operation.start_ns = event.get("started_ns", operation.start_ns)
    operation.end_ns = event.get("end_ns", operation.end_ns)
    operation.after_cluster_time = event.get("after_cluster_time")
    if event.get("status") not in {"SUCCESS", "STARTED"} and operation.operation_status == "SUCCESS":
        operation.operation_status = (
            "INDETERMINATE" if operation.kind == "write" else "UNAVAILABLE"
        )
        operation.error_message = event.get("error_message")
        operation.response_received = False
    return operation
