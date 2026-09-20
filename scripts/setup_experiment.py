"""Check the pinned toolchain and initialize the Compose replica set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"


class SetupError(RuntimeError):
    """A setup prerequisite or initialization step failed."""


def run(
    command: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
        )
    except OSError as error:
        raise SetupError(f"cannot run {' '.join(command)}: {error}") from error
    if check and result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise SetupError(f"command failed ({result.returncode}): {' '.join(command)}\n{details}")
    return result


def version_from_output(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", value)
    return match.group(1) if match else value.strip()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_revision() -> str:
    result = run(["git", "rev-parse", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def working_tree_clean() -> bool:
    result = run(["git", "status", "--porcelain"], check=False)
    return result.returncode == 0 and not result.stdout.strip()


def committed_file_revision(relative_path: str) -> str | None:
    status = run(["git", "status", "--porcelain", "--", relative_path], check=False)
    if status.returncode != 0 or status.stdout.strip():
        return None
    result = run(["git", "log", "-1", "--format=%H", "--", relative_path], check=False)
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def runtime_versions() -> dict[str, str]:
    runner = run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "run",
            "--rm",
            "runner",
            "-c",
            "import json, pymongo, sys; print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'pymongo': pymongo.version}))",
        ]
    )
    try:
        runner_versions = json.loads(runner.stdout.strip())
    except json.JSONDecodeError as error:
        raise SetupError(f"cannot parse runner versions: {runner.stdout!r}") from error
    if not isinstance(runner_versions, dict):
        raise SetupError("runner version probe returned a non-object")
    mongodb = run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "mongo1",
            "mongosh",
            "--quiet",
            "--eval",
            "db.version()",
        ]
    ).stdout.strip()
    versions = {
        "python": str(runner_versions.get("python", "")),
        "pymongo": str(runner_versions.get("pymongo", "")),
        "mongodb_server": mongodb,
    }
    expected = {"python": "3.14.7", "pymongo": "4.18.1", "mongodb_server": "7.0.34"}
    mismatches = [
        f"{name}={versions[name]} (expected {value})"
        for name, value in expected.items()
        if versions[name] != value
    ]
    if mismatches:
        raise SetupError("runtime version mismatch: " + "; ".join(mismatches))
    return versions


def check_local_tools() -> dict[str, str]:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    if python_version != "3.14.7":
        raise SetupError(f"Python 3.14.7 is required for setup; found {python_version}")
    docker_version = version_from_output(run(["docker", "version", "--format", "{{.Server.Version}}"]).stdout)
    compose_version = version_from_output(run(["docker", "compose", "version", "--short"]).stdout)
    docker_kernel = run(["docker", "info", "--format", "{{.KernelVersion}}"]).stdout.strip()
    if docker_version != "29.8.0":
        raise SetupError(f"Docker Engine 29.8.0 is required; found {docker_version}")
    if compose_version != "5.5.1":
        raise SetupError(f"Docker Compose 5.5.1 is required; found {compose_version}")
    return {
        "host_python": python_version,
        "docker_engine": docker_version,
        "docker_compose": compose_version,
        "docker_kernel": docker_kernel,
    }


def image_digest() -> list[str]:
    result = run(
        [
            "docker",
            "image",
            "inspect",
            "mongo:7.0.34",
            "--format",
            "{{json .RepoDigests}}",
        ]
    )
    try:
        values = json.loads(result.stdout.strip())
    except json.JSONDecodeError as error:
        raise SetupError(f"cannot parse MongoDB image digest: {result.stdout!r}") from error
    if not isinstance(values, list) or not values:
        raise SetupError("MongoDB image has no resolved repository digest")
    return [str(value) for value in values]


def initialize_replica_set() -> dict[str, object]:
    """Write bootstrap output to a temporary, narrowly mounted host directory."""

    with tempfile.TemporaryDirectory(prefix="mongo-consistency-setup-") as capture_dir:
        environment = os.environ.copy()
        environment["MC_RESULTS_MOUNT"] = capture_dir
        run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "run",
                "--rm",
                "runner",
                "scripts/initialize_replica_set.py",
                "--output",
                "/workspace/results/raw/replica-status.json",
            ],
            env=environment,
        )
        status_path = Path(capture_dir) / "replica-status.json"
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupError(f"cannot read replica-set initialization result: {error}") from error
    if not isinstance(payload, dict):
        raise SetupError("replica-set initialization returned a non-object")
    return payload


def setup() -> dict[str, object]:
    tools = check_local_tools()
    run(["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"])
    run(["docker", "info", "--format", "{{.ServerVersion}}"])
    run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "build",
            "runner",
            "fault-controller-1",
            "fault-controller-2",
            "fault-controller-3",
        ]
    )
    run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "up",
            "-d",
            "mongo1",
            "mongo2",
            "mongo3",
            "fault-controller-1",
            "fault-controller-2",
            "fault-controller-3",
        ]
    )
    replica_status = initialize_replica_set()
    runtime = runtime_versions()
    clean_tree = working_tree_clean()
    predictions_path = ROOT / "configs/predictions.json"
    protocol_path = ROOT / "docs/experimental-protocol.md"
    payload: dict[str, object] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "target": {
            "mongodb": "7.0.34",
            "pymongo": "4.18.1",
        },
        "actual": {
            **tools,
            **runtime,
            "mongodb_image": "mongo:7.0.34",
            "mongodb_image_digests": image_digest(),
        },
        "runner_commit": source_revision() if clean_tree else None,
        "working_tree_clean": clean_tree,
        "prediction_commit": committed_file_revision("configs/predictions.json"),
        "protocol_commit": committed_file_revision("docs/experimental-protocol.md"),
        "prediction_manifest_hash": file_hash(predictions_path),
        "protocol_hash": file_hash(protocol_path),
        "compose_file": str(COMPOSE_FILE.relative_to(ROOT)),
    }
    setup_dir = ROOT / "results/setup"
    setup_dir.mkdir(parents=True, exist_ok=True)
    (setup_dir / "replica-status.json").write_text(
        json.dumps(replica_status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (setup_dir / "toolchain.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        payload = setup()
    except SetupError as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
