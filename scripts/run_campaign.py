"""Run normal, pilot, or main histories inside the restricted runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts.package_provenance import packaged_revision
except ModuleNotFoundError:
    from package_provenance import packaged_revision
from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations, load_json
from mongo_consistency.faults import FaultControllerClient
from mongo_consistency.history import compute_history_hash, read_history, write_history
from mongo_consistency.models import History
from mongo_consistency.trial import TIMEOUT_POLICY, MongoTrial
from mongo_consistency.workloads import run_property

ROOT = Path(__file__).resolve().parents[1]
PROPERTIES = ("RYW", "MR", "MW", "WFR")


class CampaignShutdown:
    """Request a clean stop after the current trial, with a force-stop fallback."""

    def __init__(self) -> None:
        self.requested = False
        self.force_requested = False
        self._previous_handlers: dict[int, Any] = {}

    def install(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            self._previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._handle)

    def restore(self) -> None:
        for signal_number, handler in self._previous_handlers.items():
            signal.signal(signal_number, handler)
        self._previous_handlers.clear()

    def _handle(self, signal_number: int, _frame: Any) -> None:
        if self.requested:
            self.force_requested = True
            raise KeyboardInterrupt
        self.requested = True
        signal_name = signal.Signals(signal_number).name
        print(
            json.dumps(
                {
                    "status": "SHUTDOWN_REQUESTED",
                    "signal": signal_name,
                    "message": "finishing the current trial before stopping",
                },
                sort_keys=True,
            ),
            flush=True,
        )


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _committed_file_revision(root: Path, relative_path: str) -> str | None:
    packaged = packaged_revision(root)
    if packaged:
        return packaged
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", relative_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            return None
        result = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H", "--", relative_path],
            capture_output=True,
            text=True,
            encoding="ascii",
            check=False,
        )
    except OSError:
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def campaign_runtime_metadata(output_root: Path) -> dict[str, Any]:
    """Load setup provenance and frozen-input hashes available to the runner."""

    setup: dict[str, Any] = {}
    setup_candidates = [
        Path(value)
        for value in (
            os.environ.get("MC_SETUP_PROVENANCE"),
            str(output_root.parent / "setup/toolchain.json"),
            str(ROOT / "results/setup/toolchain.json"),
        )
        if value
    ]
    seen_paths: set[Path] = set()
    for setup_path in setup_candidates:
        if setup_path in seen_paths or not setup_path.is_file():
            continue
        seen_paths.add(setup_path)
        try:
            value = json.loads(setup_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            setup = value
            break
    actual = setup.get("actual", {}) if isinstance(setup.get("actual"), dict) else {}
    target = setup.get("target", {}) if isinstance(setup.get("target"), dict) else {}
    prediction_path = ROOT / "configs/predictions.json"
    protocol_path = ROOT / "docs/experimental-protocol.md"
    return {
        "software_versions": {
            "python": actual.get("python", ".".join(str(part) for part in sys.version_info[:3])),
            "pymongo": actual.get("pymongo", target.get("pymongo")),
            "docker_engine": actual.get("docker_engine"),
            "docker_compose": actual.get("docker_compose"),
            "mongodb": actual.get("mongodb_server", target.get("mongodb")),
        },
        "image_digest": actual.get("mongodb_image_digests"),
        "prediction_commit": setup.get("prediction_commit") or _committed_file_revision(ROOT, "configs/predictions.json"),
        "prediction_manifest_hash": _file_hash(prediction_path),
        "protocol_commit": setup.get("protocol_commit") or _committed_file_revision(ROOT, "docs/experimental-protocol.md"),
        "protocol_hash": setup.get("protocol_hash") or _file_hash(protocol_path),
        "runner_commit": setup.get("runner_commit"),
        "runner_dirty": setup.get("working_tree_clean") is not True,
        "checker_version": "history.v1",
        "seed_base": int(load_json(ROOT / "configs/campaign.json")["seed_base"]),
    }


def _require_frozen_provenance(campaign: str, metadata: dict[str, Any]) -> None:
    if campaign == "smoke":
        return
    required = (
        "prediction_commit",
        "prediction_manifest_hash",
        "protocol_commit",
        "protocol_hash",
        "runner_commit",
    )
    missing = [field for field in required if not metadata.get(field)]
    if metadata.get("runner_dirty"):
        missing.append("clean runner worktree")
    if missing:
        raise ValueError(
            f"{campaign} requires committed, frozen prediction/protocol/runner provenance; "
            f"missing or unclean: {', '.join(missing)}"
        )


def controller_from_environment() -> FaultControllerClient:
    return FaultControllerClient(
        endpoints={
            "mongo1": os.environ.get("FAULT_CONTROLLER_MONGO1", "http://mongo1:29091"),
            "mongo2": os.environ.get("FAULT_CONTROLLER_MONGO2", "http://mongo2:29092"),
            "mongo3": os.environ.get("FAULT_CONTROLLER_MONGO3", "http://mongo3:29093"),
        }
    )


def campaign_cases(campaign: str, configurations: dict[str, dict[str, Any]], campaign_config: dict[str, Any]) -> list[tuple[str, str]]:
    if campaign == "smoke":
        return []
    if campaign == "normal":
        repetitions = int(campaign_config["normal_repetitions"])
    elif campaign == "pilot":
        repetitions = int(campaign_config["pilot_normal_repetitions"])
    elif campaign == "experiment":
        repetitions = int(campaign_config["normal_repetitions"])
    else:
        raise ValueError(f"unknown campaign: {campaign}")
    return [
        (configuration_id, property_name)
        for _repetition in range(repetitions)
        for configuration_id in configurations
        for property_name in PROPERTIES
    ]


def adversarial_cases(
    campaign: str,
    configurations: dict[str, dict[str, Any]],
    campaign_config: dict[str, Any],
) -> list[tuple[str, str]]:
    if campaign == "pilot":
        repetitions = int(campaign_config["pilot_adversarial_repetitions"])
    elif campaign == "experiment":
        repetitions = int(campaign_config["adversarial_repetitions"])
    elif campaign == "smoke":
        repetitions = int(campaign_config["smoke_adversarial_repetitions"])
    else:
        return []
    return [
        (configuration_id, property_name)
        for _repetition in range(repetitions)
        for configuration_id in configurations
        for property_name in PROPERTIES
    ]


def campaign_plan(
    campaign: str,
    configurations: dict[str, dict[str, Any]],
    campaign_config: dict[str, Any],
    *,
    property_filter: str | None = None,
) -> list[tuple[int, str, str, bool]]:
    """Return the one deterministic, globally numbered plan for a campaign."""

    if property_filter is not None and property_filter not in PROPERTIES:
        raise ValueError(f"unknown property filter: {property_filter}")

    cases = [
        (configuration_id, property_name, False)
        for configuration_id, property_name in campaign_cases(
            campaign, configurations, campaign_config
        )
    ]
    cases.extend(
        (configuration_id, property_name, True)
        for configuration_id, property_name in adversarial_cases(
            campaign, configurations, campaign_config
        )
    )
    random.Random(int(campaign_config["seed_base"])).shuffle(cases)
    numbered = [
        (ordinal, configuration_id, property_name, adversarial)
        for ordinal, (configuration_id, property_name, adversarial) in enumerate(
            cases, start=1
        )
    ]
    if property_filter is not None:
        return [case for case in numbered if case[2] == property_filter]
    return numbered


def trial_id_for(
    campaign: str,
    ordinal: int,
    configuration_id: str,
    property_name: str,
) -> str:
    return f"{campaign}-{ordinal:05d}-{configuration_id}-{property_name.lower()}"


def schedule_id_for(property_name: str) -> str:
    return {
        "RYW": "ryw-stale-secondary",
        "MR": "mr-stale-secondary",
        "MW": "mw-election",
        "WFR": "wfr-election",
    }[property_name]


def error_history(
    *,
    trial_id: str,
    campaign: str,
    configuration: dict[str, Any],
    property_name: str,
    schedule_id: str,
    seed: int,
    error: Exception,
    runtime_metadata: dict[str, Any] | None = None,
) -> History:
    trial_id_slug = trial_id.replace("-", "_")
    return History(
        manifest={
            "schema_version": "manifest.v1",
            "trial_id": trial_id,
            "campaign_id": campaign,
            "configuration_id": configuration["id"],
            "read_concern": configuration["read_concern"],
            "write_concern": configuration["write_concern"],
            "causal_session": configuration["causal_session"],
            "property": property_name,
            "schedule_id": schedule_id,
            "seed": seed,
            "namespace": {
                "database": f"mc_{trial_id_slug}",
                "collection": "logical",
                "document_id": f"{trial_id}/x",
            },
            "timeout_policy": dict(TIMEOUT_POLICY),
            "retry_reads": False,
            "retry_writes": False,
            "runner_error": str(error),
            **dict(runtime_metadata or {}),
        },
        precondition={
            "status": "PRECONDITION_MISS",
            "checks": [
                {
                    "name": "trial-initialization",
                    "status": "PRECONDITION_MISS",
                    "expected": "trial created and initialized",
                    "actual": str(error),
                }
            ],
        },
        operations=[],
        metadata={"runner_python": sys.version},
    )


def run_case(
    *,
    campaign: str,
    ordinal: int,
    configuration: dict[str, Any],
    property_name: str,
    adversarial: bool,
    seed_uris: tuple[str, ...],
    controller: FaultControllerClient | None,
    output_root: Path,
    runtime_metadata: dict[str, Any] | None = None,
    seed_override: int | None = None,
) -> dict[str, Any]:
    metadata = dict(runtime_metadata or campaign_runtime_metadata(output_root))
    seed = (
        int(seed_override)
        if seed_override is not None
        else int(metadata["seed_base"]) + ordinal
    )
    trial_id = trial_id_for(campaign, ordinal, configuration["id"], property_name)
    schedule_id = schedule_id_for(property_name)
    history: History
    trial: MongoTrial | None = None
    trial_metadata = {key: value for key, value in metadata.items() if key != "seed_base"}
    try:
        trial = MongoTrial(
            seed_uris=seed_uris,
            configuration=configuration,
            trial_id=trial_id,
            property_name=property_name,
            schedule_id=schedule_id,
            seed=seed,
            runtime_metadata=trial_metadata,
            subtrial_deadline_seconds=TIMEOUT_POLICY["subtrial_ms"] / 1000,
        )
        trial.set_campaign(campaign)
        trial.set_adversarial(adversarial)
        with trial:
            trial.initialize()
            run_property(
                trial,
                adversarial=adversarial,
                controller=controller if adversarial else None,
            )
            history = trial.history()
    except Exception as error:  # noqa: BLE001  # Keep one record per scheduled case.
        if trial is None:
            history = error_history(
                trial_id=trial_id,
                campaign=campaign,
                configuration=configuration,
                property_name=property_name,
                schedule_id=schedule_id,
                seed=seed,
                error=error,
                runtime_metadata=trial_metadata,
            )
        else:
            trial.manifest["runner_error"] = str(error)
            trial.manifest["subtrial_finished_ns"] = time.monotonic_ns()
            if not trial.precondition.get("checks"):
                trial.mark_precondition_miss(
                    "runner-failed-before-precondition",
                    expected="property precondition checks recorded",
                    actual=str(error),
                )
            history = History(
                manifest=dict(trial.manifest),
                operations=list(trial.operations),
                precondition={
                    "status": trial.precondition["status"],
                    "checks": [dict(check) for check in trial.precondition["checks"]],
                },
                diagnostics=list(trial.diagnostics),
                final_observation=(
                    dict(trial.final_observation)
                    if trial.final_observation is not None
                    else None
                ),
                fault_events=list(trial.fault_events),
                metadata={"runner_python": sys.version},
            )
            trial.close()
    history.manifest["campaign_ordinal"] = ordinal
    history.manifest["adversarial"] = adversarial
    path = output_root / campaign / f"{trial_id}.json"
    history_hash = write_history(path, history)
    checked = check_history(history)
    return {
        "trial_id": trial_id,
        "ordinal": ordinal,
        "campaign": campaign,
        "configuration_id": configuration["id"],
        "property": property_name,
        "adversarial": adversarial,
        "seed": seed,
        "path": path.as_posix(),
        "history_hash": history_hash,
        "outcome": checked.outcome.value,
        "precondition_status": history.precondition.get("status"),
        "runner_commit": history.manifest.get("runner_commit"),
        "runner_error": history.manifest.get("runner_error"),
    }


def _record_from_history(
    path: Path,
    *,
    campaign: str,
    ordinal: int,
    configuration_id: str,
    property_name: str,
    adversarial: bool,
    seed: int,
) -> dict[str, Any]:
    """Load one completed history only when it matches the deterministic case."""

    history = read_history(path)
    manifest = history.manifest
    expected = {
        "trial_id": trial_id_for(campaign, ordinal, configuration_id, property_name),
        "campaign_id": campaign,
        "configuration_id": configuration_id,
        "property": property_name,
        "adversarial": adversarial,
        "seed": seed,
    }
    mismatches = [
        f"{field}={manifest.get(field)!r}, expected {value!r}"
        for field, value in expected.items()
        if manifest.get(field) != value
    ]
    if "campaign_ordinal" in manifest and manifest["campaign_ordinal"] != ordinal:
        mismatches.append(
            f"campaign_ordinal={manifest['campaign_ordinal']!r}, expected {ordinal!r}"
        )
    if mismatches:
        raise ValueError(f"cannot resume {path}: " + "; ".join(mismatches))
    checked = check_history(history)
    return {
        "trial_id": expected["trial_id"],
        "ordinal": ordinal,
        "campaign": campaign,
        "configuration_id": configuration_id,
        "property": property_name,
        "adversarial": adversarial,
        "seed": seed,
        "path": path.as_posix(),
        "history_hash": history.history_hash or compute_history_hash(history),
        "outcome": checked.outcome.value,
        "precondition_status": history.precondition.get("status"),
        "runner_commit": manifest.get("runner_commit"),
        "runner_error": manifest.get("runner_error"),
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Publish a manifest atomically so interruption cannot leave partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _smoke_gate(
    records_by_ordinal: dict[int, dict[str, Any]],
    *,
    planned_cases: list[tuple[int, str, str, bool]] | None = None,
) -> dict[str, Any]:
    """Require complete smoke coverage, verified preconditions, and no harness errors."""

    properties = (
        tuple(sorted({case[2] for case in planned_cases}))
        if planned_cases is not None
        else PROPERTIES
    )
    expected_counts = (
        {
            property_name: sum(case[2] == property_name for case in planned_cases)
            for property_name in properties
        }
        if planned_cases is not None
        else {property_name: 8 for property_name in properties}
    )
    expected_case_count = len(planned_cases) if planned_cases is not None else 32
    counts: dict[str, dict[str, int]] = {
        property_name: {"planned": 0, "precondition_miss": 0, "harness_error": 0}
        for property_name in properties
    }
    for record in records_by_ordinal.values():
        property_name = str(record.get("property"))
        if property_name not in counts:
            continue
        bucket = counts[property_name]
        bucket["planned"] += 1
        if record.get("precondition_status") == "PRECONDITION_MISS":
            bucket["precondition_miss"] += 1
        if record.get("outcome") == "HARNESS_ERROR":
            bucket["harness_error"] += 1
    rates = {
        property_name: (
            values["precondition_miss"] / values["planned"]
            if values["planned"]
            else 1.0
        )
        for property_name, values in counts.items()
    }
    failures = [
        f"{property_name}: precondition-miss rate {rates[property_name]:.3f} exceeds 0.05"
        for property_name in properties
        if rates[property_name] > 0.05
    ]
    if any(values["harness_error"] for values in counts.values()):
        failures.append("one or more smoke histories were classified HARNESS_ERROR")
    if len(records_by_ordinal) != expected_case_count:
        failures.append(
            "smoke coverage is incomplete: "
            f"completed {len(records_by_ordinal)} of {expected_case_count} histories"
        )
    for property_name, values in counts.items():
        if values["planned"] != expected_counts[property_name]:
            failures.append(
                f"{property_name}: expected {expected_counts[property_name]} configuration cells, "
                f"found {values['planned']}"
            )
    return {
        "passed": not failures,
        "precondition_miss_rate_by_property": rates,
        "counts_by_property": counts,
        "failures": failures,
    }


def _campaign_manifest(
    *,
    campaign: str,
    seed_base: int,
    runtime_metadata: dict[str, Any],
    records_by_ordinal: dict[int, dict[str, Any]],
    expected_case_count: int,
    started_ns: int,
    planned_ordinals: list[int] | None = None,
    status: str,
    resumed: bool,
    finished_ns: int | None = None,
    error: str | None = None,
    smoke_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planned = list(planned_ordinals or range(1, expected_case_count + 1))
    records = [records_by_ordinal[ordinal] for ordinal in sorted(records_by_ordinal)]
    runner_commits = sorted(
        {
            str(record["runner_commit"])
            for record in records
            if record.get("runner_commit")
        }
    )
    completed_ordinals = set(records_by_ordinal)
    next_ordinal = next(
        (
            ordinal
            for ordinal in planned
            if ordinal not in completed_ordinals
        ),
        None,
    )
    payload: dict[str, Any] = {
        "schema_version": "campaign-run.v1",
        "campaign": campaign,
        "status": status,
        "seed_base": seed_base,
        "expected_case_count": expected_case_count,
        "planned_ordinals": planned,
        "case_count": len(records),
        "completed_case_count": len(records),
        "next_ordinal": next_ordinal,
        "started_ns": started_ns,
        "last_updated_ns": time.monotonic_ns(),
        "software_versions": runtime_metadata["software_versions"],
        "image_digest": runtime_metadata["image_digest"],
        "prediction_commit": runtime_metadata.get("prediction_commit"),
        "prediction_manifest_hash": runtime_metadata.get("prediction_manifest_hash"),
        "protocol_commit": runtime_metadata.get("protocol_commit"),
        "protocol_hash": runtime_metadata.get("protocol_hash"),
        "runner_commit": runtime_metadata.get("runner_commit"),
        "runner_dirty": runtime_metadata.get("runner_dirty"),
        "runner_commits": runner_commits,
        "resumed": resumed,
        "records": records,
    }
    if finished_ns is not None:
        payload["finished_ns"] = finished_ns
    if error is not None:
        payload["error"] = error
    if smoke_gate is not None:
        payload["smoke_gate"] = smoke_gate
    return payload


def _check_previous_manifest(
    path: Path,
    *,
    campaign: str,
    seed_base: int,
    expected_case_count: int,
    planned_ordinals: list[int] | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read existing campaign manifest {path}: {error}") from error
    if not isinstance(payload, dict):
        raise TypeError(f"existing campaign manifest {path} is not an object")
    if payload.get("campaign") != campaign:
        raise ValueError(f"existing campaign manifest belongs to {payload.get('campaign')!r}")
    if payload.get("seed_base") != seed_base:
        raise ValueError("existing campaign manifest uses a different seed base")
    recorded_count = payload.get("expected_case_count")
    if recorded_count is not None and recorded_count != expected_case_count:
        raise ValueError("existing campaign manifest uses a different case count")
    if planned_ordinals is not None:
        recorded_plan = payload.get("planned_ordinals")
        if recorded_plan is not None and recorded_plan != planned_ordinals:
            raise ValueError("existing campaign manifest uses a different deterministic plan")
    if runtime_metadata is not None:
        provenance_fields = (
            "prediction_commit",
            "prediction_manifest_hash",
            "protocol_commit",
            "protocol_hash",
            "runner_commit",
            "software_versions",
            "image_digest",
        )
        changed = [
            field
            for field in provenance_fields
            if payload.get(field) != runtime_metadata.get(field)
        ]
        if changed:
            raise ValueError(
                "existing campaign manifest has different runtime provenance: "
                + ", ".join(changed)
            )
    if payload.get("status") == "COMPLETE" and payload.get("case_count") != expected_case_count:
        raise ValueError("completed campaign manifest is missing cases")


def run_campaign(
    campaign: str,
    output_root: Path,
    *,
    resume: bool = False,
    property_filter: str | None = None,
) -> dict[str, Any]:
    configurations = load_configurations(ROOT / "configs/configurations.json")
    campaign_config = load_json(ROOT / "configs/campaign.json")
    runtime_metadata = campaign_runtime_metadata(output_root)
    _require_frozen_provenance(campaign, runtime_metadata)
    plan = campaign_plan(
        campaign,
        configurations,
        campaign_config,
        property_filter=property_filter,
    )
    planned_ordinals = [case[0] for case in plan]
    seed_uris = tuple(
        value
        for value in os.environ.get(
            "MONGO_SEEDS",
            "mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0",
        ).split(";")
        if value
    )
    controller = controller_from_environment() if any(case[3] for case in plan) else None
    seed_base = int(campaign_config["seed_base"])
    expected_case_count = len(plan)
    campaign_dir = output_root / campaign
    manifest_path = campaign_dir / "campaign-manifest.json"
    if not resume:
        _check_previous_manifest(
            manifest_path,
            campaign=campaign,
            seed_base=seed_base,
            expected_case_count=expected_case_count,
            planned_ordinals=planned_ordinals,
            runtime_metadata=runtime_metadata,
        )
        existing_paths = [
            campaign_dir / f"{trial_id_for(campaign, ordinal, configuration_id, property_name)}.json"
            for ordinal, configuration_id, property_name, _adversarial in plan
        ]
        if any(path.is_file() for path in existing_paths):
            raise ValueError(
                f"{campaign_dir} already contains campaign histories; use --resume to continue"
            )

    if resume:
        _check_previous_manifest(
            manifest_path,
            campaign=campaign,
            seed_base=seed_base,
            expected_case_count=expected_case_count,
            planned_ordinals=planned_ordinals,
            runtime_metadata=runtime_metadata,
        )

    records_by_ordinal: dict[int, dict[str, Any]] = {}
    if resume:
        for ordinal, configuration_id, property_name, adversarial in plan:
            path = campaign_dir / f"{trial_id_for(campaign, ordinal, configuration_id, property_name)}.json"
            if path.is_file():
                records_by_ordinal[ordinal] = _record_from_history(
                    path,
                    campaign=campaign,
                    ordinal=ordinal,
                    configuration_id=configuration_id,
                    property_name=property_name,
                    adversarial=adversarial,
                    seed=seed_base + ordinal,
                )

    started = time.monotonic_ns()
    shutdown = CampaignShutdown()
    _write_json_atomic(
        manifest_path,
        _campaign_manifest(
            campaign=campaign,
            seed_base=seed_base,
            runtime_metadata=runtime_metadata,
            records_by_ordinal=records_by_ordinal,
            expected_case_count=expected_case_count,
            started_ns=started,
            planned_ordinals=planned_ordinals,
            status="RUNNING",
            resumed=resume,
        ),
    )
    interrupted = False
    fatal_error: str | None = None
    shutdown.install()
    try:
        for ordinal, configuration_id, property_name, adversarial in plan:
            if shutdown.requested:
                interrupted = True
                break
            if ordinal in records_by_ordinal:
                continue
            record = run_case(
                campaign=campaign,
                ordinal=ordinal,
                configuration=configurations[configuration_id],
                property_name=property_name,
                adversarial=adversarial,
                seed_uris=seed_uris,
                controller=controller,
                output_root=output_root,
                runtime_metadata=runtime_metadata,
            )
            records_by_ordinal[ordinal] = record
            print(json.dumps(record, sort_keys=True), flush=True)
            _write_json_atomic(
                manifest_path,
                _campaign_manifest(
                    campaign=campaign,
                    seed_base=seed_base,
                    runtime_metadata=runtime_metadata,
                    records_by_ordinal=records_by_ordinal,
                    expected_case_count=expected_case_count,
                    started_ns=started,
                    planned_ordinals=planned_ordinals,
                    status="RUNNING",
                    resumed=resume,
                ),
            )
            if shutdown.requested:
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = True
    except Exception as error:
        fatal_error = str(error)
        raise
    finally:
        shutdown.restore()
        status = "FAILED" if fatal_error is not None else "INTERRUPTED" if interrupted else "COMPLETE"
        final_manifest = _campaign_manifest(
            campaign=campaign,
            seed_base=seed_base,
            runtime_metadata=runtime_metadata,
            records_by_ordinal=records_by_ordinal,
            expected_case_count=expected_case_count,
            started_ns=started,
            planned_ordinals=planned_ordinals,
            status=status,
            resumed=resume,
            finished_ns=time.monotonic_ns(),
            error=fatal_error,
            smoke_gate=(
                _smoke_gate(records_by_ordinal, planned_cases=plan)
                if campaign == "smoke"
                else None
            ),
        )
        _write_json_atomic(manifest_path, final_manifest)
    return final_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=("smoke", "normal", "pilot", "experiment"), required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse and validate completed histories in the campaign directory",
    )
    parser.add_argument(
        "--property",
        choices=PROPERTIES,
        dest="property_filter",
        help="run only this property while retaining its global campaign ordinals",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / "results/raw")
    args = parser.parse_args()
    manifest = run_campaign(
        args.campaign,
        args.output_root,
        resume=args.resume,
        property_filter=args.property_filter,
    )
    print(
        json.dumps(
            {
                "campaign": args.campaign,
                "status": manifest["status"],
                "completed_case_count": manifest["completed_case_count"],
                "expected_case_count": manifest["expected_case_count"],
                "smoke_gate": manifest.get("smoke_gate"),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if manifest["status"] == "INTERRUPTED":
        return 130
    if args.campaign == "smoke" and not manifest.get("smoke_gate", {}).get("passed"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
