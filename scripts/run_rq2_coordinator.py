"""Run the containerized RQ2 campaign with host-only Docker fault control."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.faults import FaultControllerClient, FaultControllerError  # noqa: E402

COMPOSE_FILE = ROOT / "compose.yaml"
MEMBERS = {"mongo1", "mongo2", "mongo3"}
DEFAULT_CONTROL_PORTS = {"mongo1": 29091, "mongo2": 29092, "mongo3": 29093}
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class CoordinatorError(RuntimeError):
    """The host could not safely apply or recover a registered fault."""


class FaultCoordinator:
    """Own fault actions on the host; the runner only receives a file queue."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.active = self._load_active_state()
        endpoints = {}
        for member, default_port in DEFAULT_CONTROL_PORTS.items():
            variable = f"MC_{member.upper()}_CONTROL_HOST_PORT"
            port = int(os.environ.get(variable, default_port))
            endpoints[member] = f"http://127.0.0.1:{port}"
        self.controllers = FaultControllerClient(endpoints)

    def _load_active_state(self) -> dict[str, tuple[str, str]]:
        if not self.state_path.exists():
            return {}
        if self.state_path.is_symlink():
            raise CoordinatorError("active-fault state must not be a symlink")
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CoordinatorError(f"cannot read active-fault state: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema") != "rq2-active-faults.v1":
            raise CoordinatorError("active-fault state has an unsupported schema")
        entries = payload.get("active")
        if not isinstance(entries, list):
            raise CoordinatorError("active-fault state must contain an active list")
        active: dict[str, tuple[str, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise CoordinatorError("active-fault state contains a non-object entry")
            event_id = entry.get("event_id")
            condition = entry.get("condition")
            member = entry.get("member")
            if (
                not isinstance(event_id, str)
                or not event_id
                or not isinstance(condition, str)
                or condition not in {"F1", "F2", "F3"}
                or not isinstance(member, str)
                or member not in MEMBERS
                or event_id in active
            ):
                raise CoordinatorError("active-fault state contains an invalid or duplicate fault")
            active[event_id] = (condition, member)
        return active

    def _persist_active_state(self) -> None:
        if not self.active:
            self.state_path.unlink(missing_ok=True)
            if self.state_path.parent.exists():
                directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path.parent.chmod(0o700)
        temporary = self.state_path.with_suffix(".tmp")
        payload = {
            "schema": "rq2-active-faults.v1",
            "active": [
                {"event_id": event_id, "condition": condition, "member": member}
                for event_id, (condition, member) in sorted(self.active.items())
            ],
        }
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise CoordinatorError("request_id must be a 32-character UUID hex string")
        action = request.get("action")
        condition = request.get("condition")
        member = request.get("member")
        event_id = request.get("event_id")
        if action not in {"apply", "recover"}:
            raise CoordinatorError("action must be apply or recover")
        if condition not in {"F1", "F2", "F3"}:
            raise CoordinatorError("condition must be F1, F2, or F3")
        if member not in MEMBERS:
            raise CoordinatorError("member must be one of mongo1, mongo2, or mongo3")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 160:
            raise CoordinatorError("event_id must be a non-empty string of at most 160 characters")

        started_ns = time.monotonic_ns()
        if action == "apply":
            current_fault = self.active.get(event_id)
            if current_fault is not None and current_fault != (condition, member):
                raise CoordinatorError("event_id is already active for a different fault")
            if self.active and current_fault is None:
                raise CoordinatorError("a different fault is still active")
            self.active[event_id] = (condition, member)
            self._persist_active_state()
            details = self._apply(condition, member, event_id)
        else:
            active_fault = self.active.get(event_id)
            if active_fault is not None and active_fault != (condition, member):
                raise CoordinatorError("recovery request does not match the active fault")
            details = self._recover(condition, member, event_id)
            self.active.pop(event_id, None)
            self._persist_active_state()
        return {
            "ok": True,
            "request_id": request_id,
            "action": action,
            "condition": condition,
            "member": member,
            "event_id": event_id,
            "started_ns": started_ns,
            "finished_ns": time.monotonic_ns(),
            "details": details,
        }

    def _apply(self, condition: str, member: str, event_id: str) -> dict[str, Any]:
        if condition == "F3":
            health = self._refresh_fault_controller(member)
            if health.get("replication_isolated") is not False:
                raise CoordinatorError(
                    f"fault controller for {member} is already isolating replication traffic"
                )
            response = self.controllers.isolate(member, event_id)
            return {"mechanism": "replica-network-isolation", "controller": response}
        result = self._compose("kill", "--signal", "SIGKILL", member)
        return {"mechanism": "container-sigkill", "stdout": result.stdout.strip()}

    def _recover(self, condition: str, member: str, event_id: str) -> dict[str, Any]:
        if condition == "F3":
            refreshed = False
            try:
                response = self.controllers.heal(member, event_id)
            except FaultControllerError:
                self._refresh_fault_controller(member)
                refreshed = True
                response = self.controllers.heal(member, event_id)
            return {
                "mechanism": "replica-network-heal",
                "controller": response,
                "controller_refreshed": refreshed,
            }
        result = self._compose("start", member)
        health = self._refresh_fault_controller(member)
        if health.get("replication_isolated") is not False:
            raise CoordinatorError(
                f"fault controller for restarted {member} reports an active partition"
            )
        return {
            "mechanism": "container-start",
            "stdout": result.stdout.strip(),
            "fault_controller": health,
        }

    def _refresh_fault_controller(self, member: str) -> dict[str, Any]:
        controller_number = member.removeprefix("mongo")
        service = f"fault-controller-{controller_number}"
        self._compose("up", "-d", "--force-recreate", "--no-deps", service)
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.controllers.member_health(member)
            except FaultControllerError as error:
                last_error = error
                time.sleep(0.2)
        raise CoordinatorError(
            f"fault controller for {member} did not reattach to its current network namespace: "
            f"{last_error}"
        )

    def _compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(COMPOSE_FILE), *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CoordinatorError(f"docker compose {' '.join(arguments)} failed: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Docker error"
            raise CoordinatorError(f"docker compose {' '.join(arguments)} failed: {detail}")
        return result

    def cleanup(self) -> list[str]:
        errors = []
        for event_id, (condition, member) in reversed(list(self.active.items())):
            try:
                self._recover(condition, member, event_id)
                self.active.pop(event_id, None)
                self._persist_active_state()
            except Exception as error:  # noqa: BLE001 - report every remaining active fault.
                errors.append(f"{event_id}: {error}")
        return errors


def _write_response(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _process_requests(control_root: Path, coordinator: FaultCoordinator) -> None:
    request_root = control_root / "requests"
    response_root = control_root / "responses"
    for request_path in sorted(request_root.glob("*.json")):
        response_path = response_root / request_path.name
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise CoordinatorError("request must be a JSON object")
            if request.get("request_id") != request_path.stem:
                raise CoordinatorError("request_id does not match the request filename")
            response = coordinator.handle(request)
        except Exception as error:  # noqa: BLE001 - return the failure to the waiting runner.
            response = {
                "ok": False,
                "request_id": request_path.stem,
                "error": f"{type(error).__name__}: {error}",
            }
        _write_response(response_path, response)
        request_path.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> int:
    runner_uid = os.getuid()
    runner_gid = os.getgid()
    if runner_uid == 0:
        raise CoordinatorError("RQ2 host coordinator must run as a non-root user")
    temporary_root = ROOT / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    state_root = temporary_root / "rq2-coordinator"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.chmod(0o700)
    coordinator = FaultCoordinator(state_root / "active-faults.json")
    stale_fault_errors = coordinator.cleanup()
    if stale_fault_errors:
        raise CoordinatorError(
            "could not recover a fault left by an earlier coordinator: "
            + "; ".join(stale_fault_errors)
        )
    control_root = Path(
        tempfile.mkdtemp(prefix="mongo-consistency-rq2-control-", dir=temporary_root)
    )
    control_root.chmod(0o700)
    (control_root / "requests").mkdir()
    (control_root / "responses").mkdir()
    command = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "run",
        "--rm",
        "--no-TTY",
        "--user",
        f"{runner_uid}:{runner_gid}",
        "--volume",
        f"{control_root}:/workspace/control:rw",
        "--env",
        "MC_HOST_COORDINATOR_DIR=/workspace/control",
        "runner",
        "scripts/run_rq2_campaign.py",
    ]
    if args.resume:
        command.append("--resume")

    process: subprocess.Popen[str] | None = None
    exit_code = 1
    shutdown_signal: int | None = None
    previous_handlers: dict[int, Any] = {}

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_signal
        if shutdown_signal is None:
            shutdown_signal = signum

    for number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[number] = signal.getsignal(number)
        signal.signal(number, request_shutdown)

    try:
        process = subprocess.Popen(command, cwd=ROOT, text=True)
        shutdown_forwarded = False
        while process.poll() is None:
            _process_requests(control_root, coordinator)
            if shutdown_signal is not None and not shutdown_forwarded:
                process.send_signal(signal.SIGINT)
                shutdown_forwarded = True
            time.sleep(0.05)
        _process_requests(control_root, coordinator)
        exit_code = int(process.returncode if process.returncode is not None else 1)
        if shutdown_signal is not None and exit_code == 0:
            exit_code = 128 + shutdown_signal
    except OSError as error:
        print(f"Cannot launch RQ2 runner: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
        cleanup_errors = coordinator.cleanup()
        if cleanup_errors:
            for error in cleanup_errors:
                print(f"RQ2 fault cleanup failed: {error}", file=sys.stderr)
            exit_code = 1
        shutil.rmtree(control_root, ignore_errors=True)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="resume completed fault episodes")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
