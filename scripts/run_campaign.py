"""Run normal, pilot, or main histories inside the restricted runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mongo_consistency.config import load_configurations, load_json
from mongo_consistency.faults import FaultControllerClient
from mongo_consistency.history import write_history
from mongo_consistency.models import History, OperationRecord
from mongo_consistency.trial import TIMEOUT_POLICY, MongoTrial
from mongo_consistency.workloads import run_property

ROOT = Path(__file__).resolve().parents[1]
PROPERTIES = ("RYW", "MR", "MW", "WFR")


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
    trial_id = f"{campaign}-{ordinal:05d}-{configuration['id']}-{property_name.lower()}"
    schedule_id = {
        "RYW": "ryw-stale-secondary",
        "MR": "mr-stale-secondary",
        "MW": "mw-election",
        "WFR": "wfr-election",
    }[property_name]
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
    path = output_root / campaign / f"{trial_id}.json"
    history_hash = write_history(path, history)
    return {
        "trial_id": trial_id,
        "campaign": campaign,
        "configuration_id": configuration["id"],
        "property": property_name,
        "adversarial": adversarial,
        "seed": seed,
        "path": path.as_posix(),
        "history_hash": history_hash,
        "runner_error": history.manifest.get("runner_error"),
    }


def run_campaign(campaign: str, output_root: Path) -> dict[str, Any]:
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
    records: list[dict[str, Any]] = []
    started = time.monotonic_ns()
    for ordinal, (configuration_id, property_name, adversarial) in enumerate(cases, start=1):
        records.append(
            run_case(
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
        )
        print(json.dumps(records[-1], sort_keys=True), flush=True)
    manifest = {
        "schema_version": "campaign-run.v1",
        "campaign": campaign,
        "seed_base": int(campaign_config["seed_base"]),
        "software_versions": runtime_metadata["software_versions"],
        "image_digest": runtime_metadata["image_digest"],
        "prediction_commit": runtime_metadata["prediction_commit"],
        "prediction_manifest_hash": runtime_metadata["prediction_manifest_hash"],
        "case_count": len(records),
        "started_ns": started,
        "finished_ns": time.monotonic_ns(),
        "records": records,
    }
    manifest_path = output_root / campaign / "campaign-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=("normal", "pilot", "experiment"), required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "results/raw")
    args = parser.parse_args()
    run_campaign(args.campaign, args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
