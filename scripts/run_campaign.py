"""Run normal, pilot, or main histories inside the restricted runner."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from mongo_consistency.config import load_configurations, load_json
from mongo_consistency.history import write_history
from mongo_consistency.models import History, OperationRecord
from mongo_consistency.trial import MongoTrial
from mongo_consistency.workloads import run_property
from mongo_consistency.faults import FaultControllerClient

ROOT = Path(__file__).resolve().parents[1]
PROPERTIES = ("RYW", "MR", "MW", "WFR")


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
) -> History:
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
            "runner_error": str(error),
        },
        operations=[
            OperationRecord(
                operation_id="runner",
                kind="setup",
                key="x",
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
) -> dict[str, Any]:
    seed = 20260915 + ordinal
    trial_id = f"{campaign}-{ordinal:05d}-{configuration['id']}-{property_name.lower()}"
    schedule_id = {
        "RYW": "ryw-stale-secondary",
        "MR": "mr-stale-secondary",
        "MW": "mw-election",
        "WFR": "wfr-election",
    }[property_name]
    history: History
    try:
        with MongoTrial(
            seed_uris=seed_uris,
            configuration=configuration,
            trial_id=trial_id,
            property_name=property_name,
            schedule_id=schedule_id,
            seed=seed,
        ) as trial:
            trial.set_campaign(campaign)
            trial.initialize()
            run_property(
                trial,
                adversarial=adversarial,
                controller=controller if adversarial else None,
            )
            history = trial.history()
    except Exception as error:  # Keep one auditable record per scheduled case.
        history = error_history(
            trial_id=trial_id,
            campaign=campaign,
            configuration=configuration,
            property_name=property_name,
            schedule_id=schedule_id,
            seed=seed,
            error=error,
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
            )
        )
        print(json.dumps(records[-1], sort_keys=True), flush=True)
    manifest = {
        "schema_version": "campaign-run.v1",
        "campaign": campaign,
        "seed_base": int(campaign_config["seed_base"]),
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
