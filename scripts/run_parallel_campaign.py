"""Run deterministic campaign shards on independent Docker replica sets."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mongo_consistency.config import load_configurations, load_json
from scripts.run_campaign import (
    _campaign_manifest,
    _check_previous_manifest,
    _record_from_history,
    _write_json_atomic,
    campaign_plan,
    campaign_runtime_metadata,
    trial_id_for,
)

COMPOSE_FILE = ROOT / "compose.yaml"
DEFAULT_WORKERS = 2
MAX_WORKERS = 32
BASE_WORKER_PORT = 28000
WORKER_PORT_STRIDE = 20
WORKER_CLIENT_SUBNET_OCTET = 24


class ParallelCampaignError(RuntimeError):
    """A preflight, isolation, merge, or cleanup check failed."""


@dataclass(frozen=True)
class WorkerSpec:
    """All host and network resources owned by one campaign worker."""

    campaign: str
    index: int
    count: int
    project_name: str
    client_subnet: ipaddress.IPv4Network
    replica_subnet: ipaddress.IPv4Network
    host_ports: tuple[int, int, int, int, int, int]
    results_mount: Path
    figures_mount: Path

    def resource_manifest(self) -> dict[str, Any]:
        client_ips = {
            f"mongo{member}": str(self.client_subnet.network_address + member + 1)
            for member in range(1, 4)
        }
        replica_ips = {
            f"mongo{member}": str(self.replica_subnet.network_address + member + 1)
            for member in range(1, 4)
        }
        return {
            "index": self.index,
            "project_name": self.project_name,
            "client_subnet": str(self.client_subnet),
            "replica_subnet": str(self.replica_subnet),
            "client_ips": client_ips,
            "replica_ips": replica_ips,
            "host_ports": {
                "mongo1": self.host_ports[0],
                "mongo2": self.host_ports[1],
                "mongo3": self.host_ports[2],
                "control_mongo1": self.host_ports[3],
                "control_mongo2": self.host_ports[4],
                "control_mongo3": self.host_ports[5],
            },
            "results_directory": self.results_mount.relative_to(ROOT).as_posix(),
        }


def worker_resources(
    campaign: str,
    worker_index: int,
    worker_count: int,
    client_subnet: ipaddress.IPv4Network,
    replica_subnet: ipaddress.IPv4Network,
    host_port_base: int,
) -> WorkerSpec:
    """Build resource names and fixed addresses without touching Docker."""

    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    if worker_index < 0 or worker_index >= worker_count:
        raise ValueError("worker_index must be within worker_count")
    if client_subnet.prefixlen != 24 or replica_subnet.prefixlen != 24:
        raise ValueError("worker networks must be /24 subnets")
    if client_subnet.overlaps(replica_subnet):
        raise ValueError("client and replica subnets must be disjoint")
    directory = ROOT / "results/parallel" / campaign / f"w{worker_count:02d}-{worker_index:02d}"
    return WorkerSpec(
        campaign=campaign,
        index=worker_index,
        count=worker_count,
        project_name=f"mc-{campaign}-w{worker_count:02d}-{worker_index:02d}",
        client_subnet=client_subnet,
        replica_subnet=replica_subnet,
        host_ports=tuple(host_port_base + offset for offset in range(6)),
        results_mount=directory / "raw",
        figures_mount=directory / "figures",
    )


def worker_environment(spec: WorkerSpec) -> dict[str, str]:
    """Return the complete Compose interpolation for one isolated worker."""

    client_ips = {
        f"MC_MONGO{member}_CLIENT_IP": str(spec.client_subnet.network_address + member + 1)
        for member in range(1, 4)
    }
    replica_ips = {
        f"MC_MONGO{member}_REPLICA_IP": str(spec.replica_subnet.network_address + member + 1)
        for member in range(1, 4)
    }
    return {
        "MC_CLIENT_SUBNET": str(spec.client_subnet),
        "MC_REPLICA_SUBNET": str(spec.replica_subnet),
        **client_ips,
        **replica_ips,
        "MC_MONGO1_HOST_PORT": str(spec.host_ports[0]),
        "MC_MONGO2_HOST_PORT": str(spec.host_ports[1]),
        "MC_MONGO3_HOST_PORT": str(spec.host_ports[2]),
        "MC_MONGO1_CONTROL_HOST_PORT": str(spec.host_ports[3]),
        "MC_MONGO2_CONTROL_HOST_PORT": str(spec.host_ports[4]),
        "MC_MONGO3_CONTROL_HOST_PORT": str(spec.host_ports[5]),
        "MC_RESULTS_MOUNT": str(spec.results_mount),
        "MC_FIGURES_MOUNT": str(spec.figures_mount),
        "MC_SETUP_MOUNT": str(ROOT / "results/setup/toolchain.json"),
        "MC_SETUP_PROVENANCE": "/workspace/setup/toolchain.json",
    }


def _compose_command(project_name: str | None, *arguments: str) -> list[str]:
    command = ["docker", "compose"]
    if project_name is not None:
        command.extend(("-p", project_name))
    command.extend(("-f", str(COMPOSE_FILE)))
    command.extend(arguments)
    return command


def _run_command(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as error:
        raise ParallelCampaignError(f"cannot run {' '.join(command)}: {error}") from error
    if check and result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise ParallelCampaignError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{details}"
        )
    return result


def _docker_network_subnets() -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    listed = _run_command(["docker", "network", "ls", "-q"])
    network_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not network_ids:
        return set()
    inspected = _run_command(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            "{{range .IPAM.Config}}{{println .Subnet}}{{end}}",
            *network_ids,
        ]
    )
    subnets: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for line in inspected.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            subnets.add(ipaddress.ip_network(value, strict=False))
        except ValueError as error:
            raise ParallelCampaignError(f"Docker returned an invalid network subnet: {value}") from error
    return subnets


def _allocate_subnets(worker_count: int) -> list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Network]]:
    existing = _docker_network_subnets()
    used = set(existing)
    selected: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Network]] = []
    for pair in range(116):
        first_octet = WORKER_CLIENT_SUBNET_OCTET + pair * 2
        if first_octet + 1 > 254:
            break
        client = ipaddress.ip_network(f"172.{first_octet}.0.0/24")
        replica = ipaddress.ip_network(f"172.{first_octet + 1}.0.0/24")
        candidates = (client, replica)
        if any(candidate.overlaps(network) for candidate in candidates for network in used):
            continue
        selected.append((client, replica))
        used.update(candidates)
        if len(selected) == worker_count:
            return selected
    raise ParallelCampaignError(
        f"could not allocate {worker_count} non-overlapping Docker worker network pairs"
    )


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _allocate_port_bases(worker_count: int) -> list[int]:
    selected: list[int] = []
    candidate = BASE_WORKER_PORT
    while candidate + 5 < 65536:
        ports = tuple(candidate + offset for offset in range(6))
        if all(_port_available(port) for port in ports):
            selected.append(candidate)
            if len(selected) == worker_count:
                return selected
        candidate += WORKER_PORT_STRIDE
    raise ParallelCampaignError(
        f"could not allocate {worker_count} sets of six host ports"
    )


def _campaign_cases(
    campaign: str,
) -> tuple[dict[str, Any], list[tuple[int, str, str, bool]]]:
    configurations = load_configurations(ROOT / "configs/configurations.json")
    campaign_config = load_json(ROOT / "configs/campaign.json")
    return campaign_config, campaign_plan(campaign, configurations, campaign_config)


def _expected_paths(
    campaign: str,
    plan: list[tuple[int, str, str, bool]],
) -> dict[str, tuple[int, str, str, bool]]:
    expected: dict[str, tuple[int, str, str, bool]] = {}
    for case in plan:
        ordinal, configuration_id, property_name, _adversarial = case
        filename = f"{trial_id_for(campaign, ordinal, configuration_id, property_name)}.json"
        expected[filename] = case
    return expected


def _validate_history_directory(
    directory: Path,
    *,
    campaign: str,
    plan: list[tuple[int, str, str, bool]],
    seed_base: int,
) -> dict[int, dict[str, Any]]:
    """Validate every history in a directory and return records by global ordinal."""

    expected = _expected_paths(campaign, plan)
    records: dict[int, dict[str, Any]] = {}
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.json")):
        if path.name == "campaign-manifest.json":
            continue
        case = expected.get(path.name)
        if case is None:
            raise ParallelCampaignError(f"unexpected history in {directory}: {path.name}")
        ordinal, configuration_id, property_name, adversarial = case
        try:
            records[ordinal] = _record_from_history(
                path,
                campaign=campaign,
                ordinal=ordinal,
                configuration_id=configuration_id,
                property_name=property_name,
                adversarial=adversarial,
                seed=seed_base + ordinal,
            )
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ParallelCampaignError(f"invalid history {path}: {error}") from error
    return records


def _copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_worker(
    spec: WorkerSpec,
    *,
    campaign: str,
    plan: list[tuple[int, str, str, bool]],
    seed_base: int,
    canonical_dir: Path,
    canonical_records: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    worker_dir = spec.results_mount / campaign
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_plan = [case for case in plan if (case[0] - 1) % spec.count == spec.index]
    worker_ordinals = [case[0] for case in worker_plan]
    _check_previous_manifest(
        worker_dir / "campaign-manifest.json",
        campaign=campaign,
        seed_base=seed_base,
        expected_case_count=len(worker_plan),
        planned_ordinals=worker_ordinals,
        shard_index=spec.index,
        shard_count=spec.count,
    )
    _validate_history_directory(
        worker_dir,
        campaign=campaign,
        plan=worker_plan,
        seed_base=seed_base,
    )
    for ordinal in worker_ordinals:
        canonical_record = canonical_records.get(ordinal)
        if canonical_record is None:
            continue
        canonical_path = canonical_dir / Path(canonical_record["path"]).name
        worker_path = worker_dir / canonical_path.name
        if worker_path.is_file():
            if worker_path.read_bytes() != canonical_path.read_bytes():
                raise ParallelCampaignError(
                    f"history conflict for ordinal {ordinal}: {canonical_path} != {worker_path}"
                )
            continue
        _copy_file_atomic(canonical_path, worker_path)
    return _validate_history_directory(
        worker_dir,
        campaign=campaign,
        plan=worker_plan,
        seed_base=seed_base,
    )


def _merge_worker_histories(
    *,
    campaign: str,
    plan: list[tuple[int, str, str, bool]],
    seed_base: int,
    canonical_dir: Path,
    canonical_records: dict[int, dict[str, Any]],
    worker_specs: list[WorkerSpec],
) -> dict[int, dict[str, Any]]:
    merged = dict(canonical_records)
    plan_by_worker = {
        spec.index: {
            case[0]: case
            for case in plan
            if (case[0] - 1) % spec.count == spec.index
        }
        for spec in worker_specs
    }
    for spec in worker_specs:
        worker_dir = spec.results_mount / campaign
        worker_records = _validate_history_directory(
            worker_dir,
            campaign=campaign,
            plan=list(plan_by_worker[spec.index].values()),
            seed_base=seed_base,
        )
        for ordinal, worker_record in worker_records.items():
            case = plan_by_worker[spec.index][ordinal]
            _, configuration_id, property_name, adversarial = case
            canonical_path = canonical_dir / f"{trial_id_for(campaign, ordinal, configuration_id, property_name)}.json"
            worker_path = Path(worker_record["path"])
            if canonical_path.is_file():
                if canonical_path.read_bytes() != worker_path.read_bytes():
                    raise ParallelCampaignError(
                        f"history conflict for ordinal {ordinal}: {canonical_path} != {worker_path}"
                    )
                merged[ordinal] = _record_from_history(
                    canonical_path,
                    campaign=campaign,
                    ordinal=ordinal,
                    configuration_id=configuration_id,
                    property_name=property_name,
                    adversarial=adversarial,
                    seed=seed_base + ordinal,
                )
                continue
            _copy_file_atomic(worker_path, canonical_path)
            merged[ordinal] = _record_from_history(
                canonical_path,
                campaign=campaign,
                ordinal=ordinal,
                configuration_id=configuration_id,
                property_name=property_name,
                adversarial=adversarial,
                seed=seed_base + ordinal,
            )
    return merged


class ParallelShutdown:
    """Forward graceful and force-stop signals to all worker process groups."""

    def __init__(self) -> None:
        self.requested = False
        self.force_requested = False
        self.force_requested_at: float | None = None
        self.processes: list[subprocess.Popen[str]] = []
        self._previous_handlers: dict[int, Any] = {}
        self._lock = threading.Lock()

    def install(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            self._previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._handle)

    def restore(self) -> None:
        for signal_number, handler in self._previous_handlers.items():
            signal.signal(signal_number, handler)
        self._previous_handlers.clear()

    def _handle(self, signal_number: int, _frame: Any) -> None:
        with self._lock:
            already_requested = self.requested
            self.requested = True
            if already_requested:
                self.force_requested = True
                self.force_requested_at = time.monotonic()
        signal_name = signal.Signals(signal_number).name
        status = "FORCE_STOP_REQUESTED" if already_requested else "SHUTDOWN_REQUESTED"
        message = (
            "stopping workers after their current signal handling"
            if already_requested
            else "finishing active trials before stopping"
        )
        print(
            json.dumps({"message": message, "signal": signal_name, "status": status}, sort_keys=True),
            flush=True,
        )
        self.signal_workers(signal.SIGINT)

    def signal_workers(self, signal_number: int) -> None:
        for process in self.processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal_number)
            except ProcessLookupError:
                continue

    def force_kill_workers(self) -> None:
        self.signal_workers(signal.SIGKILL)


def _stream_worker_output(spec: WorkerSpec, process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        return
    try:
        for line in process.stdout:
            print(f"[worker {spec.index + 1}/{spec.count}] {line.rstrip()}", flush=True)
    finally:
        process.stdout.close()


def _launch_worker(
    spec: WorkerSpec,
    *,
    campaign: str,
    resume: bool,
    environment: dict[str, str],
) -> subprocess.Popen[str]:
    arguments = [
        "run",
        "--rm",
        "runner",
        "scripts/run_campaign.py",
        "--campaign",
        campaign,
        "--output-root",
        "/workspace/results",
        "--shard-index",
        str(spec.index),
        "--shard-count",
        str(spec.count),
    ]
    if resume:
        arguments.append("--resume")
    return subprocess.Popen(
        _compose_command(spec.project_name, *arguments),
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        start_new_session=True,
    )


def _wait_for_workers(
    processes: list[subprocess.Popen[str]],
    shutdown: ParallelShutdown,
) -> list[int]:
    while any(process.poll() is None for process in processes):
        if (
            shutdown.force_requested
            and shutdown.force_requested_at is not None
            and time.monotonic() - shutdown.force_requested_at > 30
        ):
            shutdown.force_kill_workers()
            shutdown.force_requested_at = None
        time.sleep(0.25)
    return [int(process.returncode or 0) for process in processes]


def _cleanup_worker(spec: WorkerSpec) -> str | None:
    result = _run_command(
        _compose_command(
            spec.project_name,
            "down",
            "--remove-orphans",
            "--volumes",
            "--timeout",
            "90",
        ),
        environment={**os.environ, **worker_environment(spec)},
        check=False,
    )
    if result.returncode:
        return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return None


def _cleanup_workers(specs: list[WorkerSpec]) -> list[str]:
    errors: list[str] = []
    for spec in specs:
        try:
            error = _cleanup_worker(spec)
        except (OSError, ParallelCampaignError) as caught:
            error = str(caught)
        if error is not None:
            errors.append(f"{spec.project_name}: {error}")
    return errors


def _stop_base_stack() -> None:
    _run_command(
        _compose_command(None, "down", "--remove-orphans"),
        environment=os.environ.copy(),
    )


def _write_global_manifest(
    *,
    campaign: str,
    plan: list[tuple[int, str, str, bool]],
    seed_base: int,
    runtime_metadata: dict[str, Any],
    records: dict[int, dict[str, Any]],
    worker_specs: list[WorkerSpec],
    worker_count: int | None = None,
    started_ns: int,
    status: str,
    resumed: bool,
    error: str | None = None,
) -> dict[str, Any]:
    execution_worker_count = worker_count if worker_count is not None else len(worker_specs)
    manifest = _campaign_manifest(
        campaign=campaign,
        seed_base=seed_base,
        runtime_metadata=runtime_metadata,
        records_by_ordinal=records,
        expected_case_count=len(plan),
        started_ns=started_ns,
        planned_ordinals=[case[0] for case in plan],
        global_expected_case_count=len(plan),
        shard_index=None,
        shard_count=execution_worker_count,
        parallel_workers=execution_worker_count,
        worker_resources=[spec.resource_manifest() for spec in worker_specs],
        status=status,
        resumed=resumed,
        finished_ns=time.monotonic_ns(),
        error=error,
    )
    _write_json_atomic(ROOT / "results/raw" / campaign / "campaign-manifest.json", manifest)
    return manifest


def run_parallel_campaign(
    campaign: str,
    *,
    workers: int = DEFAULT_WORKERS,
    resume: bool = True,
) -> dict[str, Any]:
    if workers < 1 or workers > MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
    campaign_config, plan = _campaign_cases(campaign)
    seed_base = int(campaign_config["seed_base"])
    canonical_dir = ROOT / "results/raw" / campaign
    canonical_manifest = canonical_dir / "campaign-manifest.json"
    _check_previous_manifest(
        canonical_manifest,
        campaign=campaign,
        seed_base=seed_base,
        expected_case_count=len(plan),
    )
    canonical_records = _validate_history_directory(
        canonical_dir,
        campaign=campaign,
        plan=plan,
        seed_base=seed_base,
    )
    runtime_metadata = campaign_runtime_metadata(ROOT / "results/raw")
    started = time.monotonic_ns()

    if len(canonical_records) == len(plan):
        try:
            existing_manifest = json.loads(canonical_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_manifest = {}
        if isinstance(existing_manifest, dict) and existing_manifest.get("status") == "COMPLETE":
            return existing_manifest
        return _write_global_manifest(
            campaign=campaign,
            plan=plan,
            seed_base=seed_base,
            runtime_metadata=runtime_metadata,
            records=canonical_records,
            worker_specs=[],
            worker_count=workers,
            started_ns=started,
            status="COMPLETE",
            resumed=resume,
        )

    _stop_base_stack()
    subnet_pairs = _allocate_subnets(workers)
    port_bases = _allocate_port_bases(workers)
    worker_specs = [
        worker_resources(
            campaign,
            index,
            workers,
            subnet_pairs[index][0],
            subnet_pairs[index][1],
            port_bases[index],
        )
        for index in range(workers)
    ]
    stale_cleanup_errors = _cleanup_workers(worker_specs)
    if stale_cleanup_errors:
        raise ParallelCampaignError(
            "cannot clean stale worker projects: " + "; ".join(stale_cleanup_errors)
        )
    worker_records: dict[int, dict[int, dict[str, Any]]] = {
        spec.index: _prepare_worker(
            spec,
            campaign=campaign,
            plan=plan,
            seed_base=seed_base,
            canonical_dir=canonical_dir,
            canonical_records=canonical_records,
        )
        for spec in worker_specs
    }

    shutdown = ParallelShutdown()
    processes: list[subprocess.Popen[str]] = []
    process_specs: list[WorkerSpec] = []
    output_threads: list[threading.Thread] = []
    child_codes: dict[int, int] = {}
    launch_error: str | None = None
    shutdown.install()
    try:
        for spec in worker_specs:
            assigned_ordinals = [case[0] for case in plan if (case[0] - 1) % workers == spec.index]
            if assigned_ordinals and all(
                ordinal in worker_records[spec.index] for ordinal in assigned_ordinals
            ):
                child_codes[spec.index] = 0
                continue
            environment = {**os.environ, **worker_environment(spec)}
            process = _launch_worker(
                spec,
                campaign=campaign,
                resume=resume,
                environment=environment,
            )
            processes.append(process)
            process_specs.append(spec)
            shutdown.processes = processes
            thread = threading.Thread(
                target=_stream_worker_output,
                args=(spec, process),
                name=f"campaign-worker-{spec.index}",
                daemon=True,
            )
            thread.start()
            output_threads.append(thread)
        child_codes.update(
            {
                spec.index: code
                for spec, code in zip(process_specs, _wait_for_workers(processes, shutdown))
            }
        )
    except KeyboardInterrupt:
        shutdown.requested = True
        shutdown.signal_workers(signal.SIGINT)
        if processes:
            child_codes.update(
                {
                    spec.index: code
                    for spec, code in zip(process_specs, _wait_for_workers(processes, shutdown))
                }
            )
    except (OSError, ParallelCampaignError, TypeError, ValueError) as error:
        launch_error = str(error) or error.__class__.__name__
        shutdown.requested = True
        shutdown.signal_workers(signal.SIGINT)
        if processes:
            child_codes.update(
                {
                    spec.index: code
                    for spec, code in zip(process_specs, _wait_for_workers(processes, shutdown))
                }
            )
    finally:
        for thread in output_threads:
            thread.join(timeout=2)
        shutdown.restore()

    merge_error: str | None = None
    merged_records = dict(canonical_records)
    try:
        merged_records = _merge_worker_histories(
            campaign=campaign,
            plan=plan,
            seed_base=seed_base,
            canonical_dir=canonical_dir,
            canonical_records=canonical_records,
            worker_specs=worker_specs,
        )
    except (OSError, UnicodeError, ValueError, ParallelCampaignError, json.JSONDecodeError) as error:
        merge_error = str(error)

    cleanup_errors = _cleanup_workers(worker_specs)
    codes = list(child_codes.values())
    interrupted = shutdown.requested or any(code in {130, 143} for code in codes)
    nonzero = [f"worker {index} exited {code}" for index, code in child_codes.items() if code]
    missing_count = len(plan) - len(merged_records)
    errors = [*nonzero]
    if missing_count:
        errors.append(f"{missing_count} planned histories are missing")
    if cleanup_errors:
        errors.extend(f"cleanup failed: {error}" for error in cleanup_errors)
    if merge_error is not None:
        errors.append(f"merge failed: {merge_error}")
    if launch_error is not None:
        errors.append(f"worker launch failed: {launch_error}")
    if (
        merge_error is not None
        or cleanup_errors
        or launch_error is not None
        or (nonzero and not interrupted)
        or (missing_count and not interrupted)
    ):
        status = "FAILED"
    elif interrupted:
        status = "INTERRUPTED"
    else:
        status = "COMPLETE"
    return _write_global_manifest(
        campaign=campaign,
        plan=plan,
        seed_base=seed_base,
        runtime_metadata=runtime_metadata,
        records=merged_records,
        worker_specs=worker_specs,
        started_ns=started,
        status=status,
        resumed=resume,
        error="; ".join(errors) if errors else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=("normal", "pilot", "experiment"), required=True)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="reuse and validate existing worker histories (default: enabled)",
    )
    args = parser.parse_args()
    try:
        manifest = run_parallel_campaign(
            args.campaign,
            workers=args.workers,
            resume=args.resume,
        )
    except (ParallelCampaignError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"campaign": args.campaign, "status": "FAILED", "error": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "campaign": args.campaign,
                "status": manifest["status"],
                "completed_case_count": manifest["completed_case_count"],
                "expected_case_count": manifest["expected_case_count"],
                "workers": manifest.get("parallel_workers", 0),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 130 if manifest["status"] == "INTERRUPTED" else 0 if manifest["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
