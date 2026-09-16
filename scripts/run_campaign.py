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

from mongo_consistency.config import load_configurations, load_json
from mongo_consistency.faults import FaultControllerClient
from mongo_consistency.history import compute_history_hash, read_history, write_history
from mongo_consistency.models import History, OperationRecord
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


def _source_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="ascii",
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def campaign_runtime_metadata(output_root: Path) -> dict[str, Any]:
    """Load setup provenance and frozen-input hashes available to the runner."""

    setup_path = output_root.parent / "setup/toolchain.json"
    setup: dict[str, Any] = {}
    if setup_path.is_file():
        try:
            value = json.loads(setup_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                setup = value
        except (OSError, UnicodeError, json.JSONDecodeError):
            setup = {}
    actual = setup.get("actual", {}) if isinstance(setup.get("actual"), dict) else {}
    prediction_path = ROOT / "configs/predictions.json"
    return {
        "software_versions": {
            "python": actual.get("python", ".".join(str(part) for part in sys.version_info[:3])),
            "pymongo": "4.18.1",
            "docker_engine": actual.get("docker_engine"),
            "docker_compose": actual.get("docker_compose"),
            "mongodb": "7.0.34",
        },
        "image_digest": actual.get("mongodb_image_digests"),
        "prediction_commit": setup.get("source_revision") or _source_revision(ROOT),
        "prediction_manifest_hash": setup.get("prediction_manifest_hash") or _file_hash(prediction_path),
        "runner_version": setup.get("source_revision") or _source_revision(ROOT),
        "checker_version": "history.v1",
        "seed_base": int(load_json(ROOT / "configs/campaign.json")["seed_base"]),
    }


def controller_from_environment() -> FaultControllerClient:
    return FaultControllerClient(
        endpoints={
            "mongo1": os.environ.get("FAULT_CONTROLLER_MONGO1", "http://mongo1:29091"),
            "mongo2": os.environ.get("FAULT_CONTROLLER_MONGO2", "http://mongo2:29092"),
            "mongo3": os.environ.get("FAULT_CONTROLLER_MONGO3", "http://mongo3:29093"),
        }
    )


def campaign_cases(campaign: str, configurations: dict[str, dict[str, Any]], campaign_config: dict[str, Any]) -> list[tuple[str, str]]:
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
    else:
        return []
    return [
        (configuration_id, property_name)
        for _repetition in range(repetitions)
        for configuration_id in configurations
        for property_name in PROPERTIES
    ]


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
        operations=[
            OperationRecord(
                operation_id="runner",
                kind="setup",
                key="x",
                trial_id=trial_id,
                property=property_name,
                operation_status="HARNESS_ERROR",
                error_message=str(error),
            )
        ],
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
) -> dict[str, Any]:
    metadata = dict(runtime_metadata or campaign_runtime_metadata(output_root))
    seed = int(metadata["seed_base"]) + ordinal
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
        with trial:
            trial.set_campaign(campaign)
            trial.set_adversarial(adversarial)
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
            trial.close()
            trial.manifest["runner_error"] = str(error)
            trial.manifest["subtrial_finished_ns"] = time.monotonic_ns()
            trial.operations.append(
                OperationRecord(
                    operation_id="runner",
                    kind="setup",
                    key="x",
                    trial_id=trial_id,
                    property=property_name,
                    operation_status="HARNESS_ERROR",
                    error_message=str(error),
                )
            )
            history = History(
                manifest=dict(trial.manifest),
                operations=list(trial.operations),
                fault_events=list(trial.fault_events),
                metadata={"runner_python": sys.version},
            )
    history.manifest["campaign_ordinal"] = ordinal
    history.manifest["adversarial"] = adversarial
    path = output_root / campaign / f"{trial_id}.json"
    history_hash = write_history(path, history)
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
        "runner_version": history.manifest.get("runner_version"),
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
    if mismatches:
        raise ValueError(f"cannot resume {path}: " + "; ".join(mismatches))
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
        "runner_version": manifest.get("runner_version"),
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


def _campaign_manifest(
    *,
    campaign: str,
    seed_base: int,
    runtime_metadata: dict[str, Any],
    records_by_ordinal: dict[int, dict[str, Any]],
    expected_case_count: int,
    started_ns: int,
    status: str,
    resumed: bool,
    finished_ns: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    records = [records_by_ordinal[ordinal] for ordinal in sorted(records_by_ordinal)]
    runner_versions = sorted(
        {
            str(record["runner_version"])
            for record in records
            if record.get("runner_version")
        }
    )
    completed_ordinals = set(records_by_ordinal)
    next_ordinal = next(
        (
            ordinal
            for ordinal in range(1, expected_case_count + 1)
            if ordinal not in completed_ordinals
        ),
        expected_case_count + 1,
    )
    payload: dict[str, Any] = {
        "schema_version": "campaign-run.v1",
        "campaign": campaign,
        "status": status,
        "seed_base": seed_base,
        "expected_case_count": expected_case_count,
        "case_count": len(records),
        "completed_case_count": len(records),
        "next_ordinal": next_ordinal,
        "started_ns": started_ns,
        "last_updated_ns": time.monotonic_ns(),
        "software_versions": runtime_metadata["software_versions"],
        "image_digest": runtime_metadata["image_digest"],
        "prediction_commit": runtime_metadata["prediction_commit"],
        "prediction_manifest_hash": runtime_metadata["prediction_manifest_hash"],
        "runner_versions": runner_versions,
        "resumed": resumed,
        "records": records,
    }
    if finished_ns is not None:
        payload["finished_ns"] = finished_ns
    if error is not None:
        payload["error"] = error
    return payload


def _check_previous_manifest(
    path: Path,
    *,
    campaign: str,
    seed_base: int,
    expected_case_count: int,
) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read existing campaign manifest {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"existing campaign manifest {path} is not an object")
    if payload.get("campaign") != campaign:
        raise ValueError(f"existing campaign manifest belongs to {payload.get('campaign')!r}")
    if payload.get("seed_base") != seed_base:
        raise ValueError("existing campaign manifest uses a different seed base")
    recorded_count = payload.get("expected_case_count")
    if recorded_count is not None and recorded_count != expected_case_count:
        raise ValueError("existing campaign manifest uses a different case count")
    if payload.get("status") == "COMPLETE" and payload.get("case_count") != expected_case_count:
        raise ValueError("completed campaign manifest is missing cases")


def run_campaign(
    campaign: str,
    output_root: Path,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    configurations = load_configurations(ROOT / "configs/configurations.json")
    campaign_config = load_json(ROOT / "configs/campaign.json")
    runtime_metadata = campaign_runtime_metadata(output_root)
    cases = [(configuration_id, property_name, False) for configuration_id, property_name in campaign_cases(campaign, configurations, campaign_config)]
    cases.extend(
        (configuration_id, property_name, True)
        for configuration_id, property_name in adversarial_cases(campaign, configurations, campaign_config)
    )
    rng = random.Random(int(campaign_config["seed_base"]))
    rng.shuffle(cases)
    seed_uris = tuple(
        value
        for value in os.environ.get(
            "MONGO_SEEDS",
            "mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0",
        ).split(";")
        if value
    )
    controller = controller_from_environment() if any(case[2] for case in cases) else None
    seed_base = int(campaign_config["seed_base"])
    expected_case_count = len(cases)
    campaign_dir = output_root / campaign
    manifest_path = campaign_dir / "campaign-manifest.json"
    if not resume:
        _check_previous_manifest(
            manifest_path,
            campaign=campaign,
            seed_base=seed_base,
            expected_case_count=expected_case_count,
        )
        existing_paths = [
            campaign_dir / f"{trial_id_for(campaign, ordinal, configuration_id, property_name)}.json"
            for ordinal, (configuration_id, property_name, _adversarial) in enumerate(cases, start=1)
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
        )

    records_by_ordinal: dict[int, dict[str, Any]] = {}
    if resume:
        for ordinal, (configuration_id, property_name, adversarial) in enumerate(cases, start=1):
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
            status="RUNNING",
            resumed=resume,
        ),
    )
    interrupted = False
    fatal_error: str | None = None
    shutdown.install()
    try:
        for ordinal, (configuration_id, property_name, adversarial) in enumerate(cases, start=1):
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
                    status="RUNNING",
                    resumed=resume,
                ),
            )
            if shutdown.requested:
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = True
    except Exception as error:  # noqa: BLE001
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
            status=status,
            resumed=resume,
            finished_ns=time.monotonic_ns(),
            error=fatal_error,
        )
        _write_json_atomic(manifest_path, final_manifest)
    return final_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=("normal", "pilot", "experiment"), required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse and validate completed histories in the campaign directory",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / "results/raw")
    args = parser.parse_args()
    manifest = run_campaign(args.campaign, args.output_root, resume=args.resume)
    print(
        json.dumps(
            {
                "campaign": args.campaign,
                "status": manifest["status"],
                "completed_case_count": manifest["completed_case_count"],
                "expected_case_count": manifest["expected_case_count"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 130 if manifest["status"] == "INTERRUPTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
