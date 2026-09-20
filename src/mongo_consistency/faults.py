"""Client for the isolated, HTTP-only fault-controller sidecars."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class FaultControllerError(RuntimeError):
    """A fault controller could not apply or verify an event."""


@dataclass(frozen=True)
class FaultControllerClient:
    """Call one Compose sidecar without access to Docker APIs."""

    endpoints: dict[str, str]
    timeout_seconds: float = 5.0

    def _request(self, member: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        endpoint = self.endpoints.get(member)
        if endpoint is None:
            raise FaultControllerError(f"no fault-controller endpoint for {member}")
        data = None
        headers: dict[str, str] = {}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise FaultControllerError(
                f"fault controller request failed for {member}: "
                f"HTTP {error.code}: {body or error.reason}"
            ) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise FaultControllerError(f"fault controller request failed for {member}: {error}") from error
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise FaultControllerError(f"fault controller rejected {member}: {result!r}")
        return result

    def health(self) -> dict[str, dict[str, Any]]:
        return {member: self._request(member, "/health") for member in self.endpoints}

    def isolate(self, member: str, event_id: str) -> dict[str, Any]:
        response = self._request(
            member,
            "/apply",
            {"action": "isolate", "event_id": event_id},
        )
        if (
            response.get("member") != member
            or response.get("event_id") != event_id
            or response.get("verified") is not True
            or response.get("replication_isolated") is not True
        ):
            raise FaultControllerError(f"isolation was not verified for {member}: {response!r}")
        return response

    def heal(self, member: str, event_id: str) -> dict[str, Any]:
        response = self._request(
            member,
            "/apply",
            {"action": "heal", "event_id": event_id},
        )
        if (
            response.get("member") != member
            or response.get("event_id") != event_id
            or response.get("verified") is not True
            or response.get("replication_isolated") is not False
        ):
            raise FaultControllerError(f"healing was not verified for {member}: {response!r}")
        return response

    def isolate_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        return [self.isolate(member, event_id) for member in members]

    def heal_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        return [self.heal(member, event_id) for member in members]


@dataclass(frozen=True)
class HostFaultCoordinatorClient:
    """Request host-only node control through a mounted file queue."""

    control_directory: Path
    timeout_seconds: float = 90.0
    poll_seconds: float = 0.05

    @classmethod
    def from_environment(cls) -> HostFaultCoordinatorClient:
        value = os.environ.get("MC_HOST_COORDINATOR_DIR")
        if not value:
            raise FaultControllerError("MC_HOST_COORDINATOR_DIR is required for RQ2")
        return cls(Path(value))

    def apply(self, condition: str, member: str, event_id: str) -> dict[str, Any]:
        return self._request("apply", condition, member, event_id)

    def recover(self, condition: str, member: str, event_id: str) -> dict[str, Any]:
        return self._request("recover", condition, member, event_id)

    def _request(
        self,
        action: str,
        condition: str,
        member: str,
        event_id: str,
    ) -> dict[str, Any]:
        request_id = uuid4().hex
        request_directory = self.control_directory / "requests"
        response_directory = self.control_directory / "responses"
        request_directory.mkdir(parents=True, exist_ok=True)
        response_directory.mkdir(parents=True, exist_ok=True)
        request_path = request_directory / f"{request_id}.json"
        temporary_path = request_path.with_suffix(".tmp")
        response_path = response_directory / f"{request_id}.json"
        payload = {
            "request_id": request_id,
            "action": action,
            "condition": condition,
            "member": member,
            "event_id": event_id,
            "requested_at_ns": time.monotonic_ns(),
        }
        temporary_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary_path, request_path)

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if response_path.is_file():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise FaultControllerError(
                        f"host coordinator returned an invalid response: {error}"
                    ) from error
                try:
                    response_path.unlink()
                except FileNotFoundError:
                    pass
                if not isinstance(response, dict) or response.get("ok") is not True:
                    raise FaultControllerError(
                        f"host coordinator rejected {action} for {member}: {response!r}"
                    )
                expected = {
                    "request_id": request_id,
                    "action": action,
                    "condition": condition,
                    "member": member,
                    "event_id": event_id,
                }
                if any(response.get(field) != value for field, value in expected.items()):
                    raise FaultControllerError(
                        f"host coordinator response did not match request {request_id}"
                    )
                return response
            time.sleep(self.poll_seconds)

        raise FaultControllerError(
            f"host coordinator timed out during {action} for {member} ({event_id})"
        )
