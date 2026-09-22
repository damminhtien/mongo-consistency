"""Check and record the runner container's isolation boundary."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"
DEFAULT_OUTPUT = ROOT / "results/validation/runner-isolation.json"
ALLOWED_TARGETS = {
    "/workspace/results/raw",
    "/workspace/figures",
    "/workspace/setup/toolchain.json",
}
ALLOWED_SOURCES = {
    (ROOT / "results/raw").resolve(),
    (ROOT / "figures").resolve(),
    (ROOT / "results/setup/toolchain.json").resolve(),
}
FORBIDDEN_ENV = re.compile(
    r"^(?:DOCKER_|REGISTRY_AUTH_FILE$|AWS_.*|GOOGLE_APPLICATION_CREDENTIALS$)"
    r"|CREDENTIAL",
    re.IGNORECASE,
)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one Docker command without exposing command output in the evidence."""

    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    details = (result.stderr or result.stdout).strip()
    return details or f"command exited with status {result.returncode}"


def _display_source(source: Path) -> str:
    try:
        return source.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return source.as_posix()


def _config_probe() -> dict[str, Any]:
    result = run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"]
    )
    if result.returncode:
        raise RuntimeError(f"Compose config failed: {_command_error(result)}")
    try:
        config = json.loads(result.stdout)
        service = config["services"]["runner"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Compose config did not contain a runner service: {error}") from error

    serialized = json.dumps(service, sort_keys=True).lower()
    if "docker.sock" in serialized:
        raise RuntimeError("runner Compose service contains a Docker socket reference")

    volumes = service.get("volumes", [])
    mounts = []
    invalid_mounts = []
    for volume in volumes:
        if not isinstance(volume, dict):
            invalid_mounts.append(repr(volume))
            continue
        source = Path(str(volume.get("source", ""))).resolve()
        target = str(volume.get("target", ""))
        mounts.append({"source": _display_source(source), "target": target})
        if target not in ALLOWED_TARGETS or source not in ALLOWED_SOURCES:
            invalid_mounts.append({"source": _display_source(source), "target": target})
    if invalid_mounts:
        raise RuntimeError(f"runner has a mount outside the whitelist: {invalid_mounts}")

    environment = service.get("environment", {})
    environment_names = sorted(str(name) for name in environment)
    forbidden_environment = [name for name in environment_names if FORBIDDEN_ENV.search(name)]
    if forbidden_environment:
        raise RuntimeError(f"runner Compose environment contains credential names: {forbidden_environment}")

    expected_targets = sorted(ALLOWED_TARGETS)
    actual_targets = sorted(item["target"] for item in mounts)
    if actual_targets != expected_targets:
        raise RuntimeError(
            f"runner mount targets differ from the whitelist: {actual_targets!r}"
        )

    return {
        "docker_socket_reference": False,
        "mounts": mounts,
        "mount_targets": actual_targets,
        "environment_names": environment_names,
        "forbidden_environment_names": [],
        "network_names": sorted(str(name) for name in service.get("networks", {})),
        "read_only_root": service.get("read_only") is True,
        "cap_drop_all": "ALL" in service.get("cap_drop", []),
    }


def _runtime_probe() -> dict[str, Any]:
    probe = (
        "import json, os; from pathlib import Path; "
        "names=sorted(k for k in os.environ if k.startswith('DOCKER_') "
        "or 'CREDENTIAL' in k.upper() or k in {'REGISTRY_AUTH_FILE', 'AWS_ACCESS_KEY_ID', "
        "'AWS_SECRET_ACCESS_KEY', 'GOOGLE_APPLICATION_CREDENTIALS'}); "
        "paths=['/root/.docker/config.json','/home/runner/.docker/config.json',"
        "'/workspace/.docker/config.json']; "
        "print(json.dumps({'docker_socket_exists': Path('/var/run/docker.sock').exists(), "
        "'credential_environment_names': names, "
        "'credential_files_present': [p for p in paths if Path(p).exists()]}))"
    )
    result = run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "run",
            "--rm",
            "--no-deps",
            "--build",
            "runner",
            "-c",
            probe,
        ]
    )
    if result.returncode:
        raise RuntimeError(f"runner runtime probe failed: {_command_error(result)}")
    try:
        output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        payload = json.loads(output_lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"runner runtime probe returned invalid JSON: {error}") from error
    except IndexError as error:
        raise RuntimeError("runner runtime probe returned no JSON") from error
    if payload.get("docker_socket_exists"):
        raise RuntimeError("runner contains /var/run/docker.sock")
    if payload.get("credential_environment_names"):
        raise RuntimeError(
            "runner contains credential environment names: "
            + repr(payload["credential_environment_names"])
        )
    if payload.get("credential_files_present"):
        raise RuntimeError(
            "runner contains credential files: " + repr(payload["credential_files_present"])
        )
    return payload


def check_isolation() -> dict[str, Any]:
    """Run static Compose and runtime container isolation checks."""

    return {
        "compose": _config_probe(),
        "runtime": _runtime_probe(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    payload: dict[str, Any] = {
        "schema_version": "runner-isolation.v1",
        "compose_file": "compose.yaml",
        "status": "ERROR",
    }
    try:
        payload["checks"] = check_isolation()
        payload["status"] = "PASS"
    except (OSError, RuntimeError) as error:
        payload["error"] = str(error)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
