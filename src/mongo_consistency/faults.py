"""Client for the isolated, HTTP-only fault-controller sidecars."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


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
        return self._request(
            member,
            "/apply",
            {"action": "isolate", "event_id": event_id},
        )

    def heal(self, member: str, event_id: str) -> dict[str, Any]:
        return self._request(
            member,
            "/apply",
            {"action": "heal", "event_id": event_id},
        )

    def isolate_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        return [self.isolate(member, event_id) for member in members]

    def heal_many(self, members: list[str], event_id: str) -> list[dict[str, Any]]:
        return [self.heal(member, event_id) for member in members]
