"""Run the three preregistered, matched-seed RQ3 mechanism contrasts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_campaign import (
    _require_frozen_provenance,
    _write_json_atomic,
    campaign_runtime_metadata,
    controller_from_environment,
    run_case,
    trial_id_for,
)

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations
from mongo_consistency.faults import FaultControllerClient
from mongo_consistency.history import read_history
from mongo_consistency.rq3 import (
    TOPOLOGY_PLANS,
    arm_control_errors,
    normalize_topology,
    pair_control,
)
from mongo_consistency.rq3_anchors import verify_anchor_manifest
from mongo_consistency.topology import TopologyOracle

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPETITIONS = 5
MIN_REPETITIONS = 5
MAX_REPETITIONS = 10


@dataclass(frozen=True)
class Contrast:
    contrast_id: str
    mechanism: str
    left_configuration: str
    right_configuration: str
    property_name: str
    changed_factor: str
    capture_operations: tuple[str, ...]


CONTRASTS = (
    Contrast(
        contrast_id="M1",
        mechanism="causal session",
        left_configuration="C5",
        right_configuration="C6",
        property_name="RYW",
        changed_factor="causal_session: false -> true",
        capture_operations=("write", "read"),
    ),
    Contrast(
        contrast_id="M2",
        mechanism="read concern",
        left_configuration="C8",
        right_configuration="C5",
        property_name="WFR",
        changed_factor="read_concern: local -> majority",
        capture_operations=("read", "write"),
    ),
    Contrast(
        contrast_id="M3",
        mechanism="write concern",
        left_configuration="C3",
        right_configuration="C6",
        property_name="MW",
        changed_factor="write_concern: w:1 -> majority",
        capture_operations=("first_write", "second_write"),
    ),
)


@dataclass(frozen=True)
class Case:
    contrast: Contrast
    replicate: int
    pair_seed: int
    ordinal: int
    configuration_id: str

    @property
    def campaign(self) -> str:
        return f"rq3-{self.contrast.contrast_id.lower()}"

    @property
    def pair_id(self) -> str:
        return f"{self.contrast.contrast_id.lower()}-r{self.replicate:02d}"

    @property
    def trial_id(self) -> str:
        return trial_id_for(
            self.campaign,
            self.ordinal,
            self.configuration_id,
            self.contrast.property_name,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_configuration_contrasts(
    configurations: dict[str, dict[str, Any]],
) -> None:
    expected = {
        "M1": (
            "causal_session",
            {"read_concern": "majority", "write_concern": "majority", "causal_session": False},
            {"read_concern": "majority", "write_concern": "majority", "causal_session": True},
        ),
        "M2": (
            "read_concern",
            {"read_concern": "local", "write_concern": "majority", "causal_session": False},
            {"read_concern": "majority", "write_concern": "majority", "causal_session": False},
        ),
        "M3": (
            "write_concern",
            {"read_concern": "majority", "write_concern": "w:1", "causal_session": True},
            {"read_concern": "majority", "write_concern": "majority", "causal_session": True},
        ),
    }
    for contrast in CONTRASTS:
        left = configurations[contrast.left_configuration]
        right = configurations[contrast.right_configuration]
        field, left_expected, right_expected = expected[contrast.contrast_id]
        for configuration, expected_values in (
            (left, left_expected),
            (right, right_expected),
        ):
            actual_values = {
                key: configuration.get(key)
                for key in ("read_concern", "write_concern", "causal_session")
            }
            if actual_values != expected_values:
                raise ValueError(
                    f"{contrast.contrast_id} configuration {configuration.get('id')} "
                    f"has settings {actual_values!r}, expected {expected_values!r}"
                )
        left_other = {key: value for key, value in left.items() if key not in {"id", field}}
        right_other = {key: value for key, value in right.items() if key not in {"id", field}}
        if left_other != right_other:
            raise ValueError(
                f"{contrast.contrast_id} must differ only in {field}; "
                f"other configuration fields differ"
            )


def _plan(repetitions: int, seed_base: int) -> list[Case]:
    cases: list[Case] = []
    for contrast_index, contrast in enumerate(CONTRASTS, start=1):
        ordinal = 0
        for replicate in range(1, repetitions + 1):
            pair_seed = seed_base + contrast_index * 100 + replicate
            arms = [contrast.left_configuration, contrast.right_configuration]
            if replicate % 2 == 0:
                arms.reverse()
            for configuration_id in arms:
                ordinal += 1
                cases.append(
                    Case(
                        contrast=contrast,
                        replicate=replicate,
                        pair_seed=pair_seed,
                        ordinal=ordinal,
                        configuration_id=configuration_id,
                    )
                )
    return cases


def _record_for_history(
    case: Case, path: Path, metadata: dict[str, Any]
) -> dict[str, Any]:
    history = read_history(path)
    expected = {
        "trial_id": case.trial_id,
        "campaign_id": case.campaign,
        "configuration_id": case.configuration_id,
        "property": case.contrast.property_name,
        "seed": case.pair_seed,
        "rq3_contrast_id": case.contrast.contrast_id,
        "rq3_pair_id": case.pair_id,
        "rq3_pair_seed": case.pair_seed,
        "runner_commit": metadata.get("runner_commit"),
        "runner_dirty": False,
        "prediction_commit": metadata.get("prediction_commit"),
        "prediction_manifest_hash": metadata.get("prediction_manifest_hash"),
        "protocol_commit": metadata.get("protocol_commit"),
        "protocol_hash": metadata.get("protocol_hash"),
        "software_versions": metadata.get("software_versions"),
        "image_digest": metadata.get("image_digest"),
        "checker_version": metadata.get("checker_version"),
        "rq3_protocol_id": "rq3-protocol.v2",
        "rq3_topology_plan": TOPOLOGY_PLANS[case.contrast.contrast_id].to_dict(),
    }
    mismatches = [
        f"{key}={history.manifest.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if history.manifest.get(key) != value
    ]
    if mismatches:
        raise ValueError(f"cannot resume {path}: " + "; ".join(mismatches))
    return {
        "trial_id": case.trial_id,
        "ordinal": case.ordinal,
        "campaign": case.campaign,
        "contrast_id": case.contrast.contrast_id,
        "pair_id": case.pair_id,
        "replicate": case.replicate,
        "pair_seed": case.pair_seed,
        "configuration_id": case.configuration_id,
        "property": case.contrast.property_name,
        "adversarial": True,
        "seed": case.pair_seed,
        "path": path.relative_to(ROOT).as_posix()
        if path.is_relative_to(ROOT)
        else path.as_posix(),
        "history_hash": history.history_hash,
        "outcome": check_history(history).outcome.value,
        "precondition_status": history.precondition.get("status"),
        "runner_commit": history.manifest.get("runner_commit"),
        "runner_error": history.manifest.get("runner_error"),
    }


def _contrast_rows(repetitions: int) -> list[dict[str, Any]]:
    return [
        {
            "contrast_id": contrast.contrast_id,
            "mechanism": contrast.mechanism,
            "left_configuration": contrast.left_configuration,
            "right_configuration": contrast.right_configuration,
            "property": contrast.property_name,
            "changed_factor": contrast.changed_factor,
            "capture_operations": list(contrast.capture_operations),
            "pair_ids": [
                f"{contrast.contrast_id.lower()}-r{replicate:02d}"
                for replicate in range(1, repetitions + 1)
            ],
        }
        for contrast in CONTRASTS
    ]


def _manifest_payload(
    *,
    status: str,
    repetitions: int,
    seed_base: int,
    metadata: dict[str, Any],
    cases: list[Case],
    records: dict[str, dict[str, Any]],
    configurations: dict[str, dict[str, Any]],
    started_ns: int,
    preflight_sha256: str,
    anchor_manifest_sha256: str,
    finished_ns: int | None = None,
) -> dict[str, Any]:
    pair_controls = _pair_control_rows(cases, records, configurations)
    payload: dict[str, Any] = {
        "schema_version": "rq3-campaign.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign": "rq3",
        "status": status,
        "repetitions_per_contrast": repetitions,
        "seed_base": seed_base,
        "planned_case_count": len(cases),
        "completed_case_count": len(records),
        "runner_commit": metadata.get("runner_commit"),
        "runner_script_sha256": _sha256(Path(__file__)),
        "configuration_sha256": _sha256(ROOT / "configs/configurations.json"),
        "protocol_sha256": metadata.get("protocol_hash"),
        "topology_plan_sha256": _sha256(ROOT / "src/mongo_consistency/rq3.py"),
        "preflight_sha256": preflight_sha256,
        "anchor_manifest_sha256": anchor_manifest_sha256,
        "runtime_provenance": _runtime_provenance(metadata),
        "contrasts": _contrast_rows(repetitions),
        "pair_controls": pair_controls,
        "records": sorted(records.values(), key=lambda item: (item["contrast_id"], item["ordinal"])),
        "started_ns": started_ns,
        "last_updated_ns": time.monotonic_ns(),
    }
    if finished_ns is not None:
        payload["finished_ns"] = finished_ns
    return payload


def _pair_control_rows(
    cases: list[Case],
    records: dict[str, dict[str, Any]],
    configurations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for case in cases:
        record = records.get(case.trial_id)
        history: dict[str, Any] | None = None
        if record is not None:
            path = Path(str(record["path"]))
            if not path.is_absolute():
                path = ROOT / path
            if path.is_file():
                history = read_history(path).to_dict()
        arms = by_pair.setdefault((case.contrast.contrast_id, case.pair_id), {})
        if history is not None:
            arms[case.configuration_id] = history
    return [
        pair_control(contrast_id, pair_id, arms, configurations=configurations)
        for (contrast_id, pair_id), arms in sorted(by_pair.items())
    ]


def _pair_is_complete(pair_control_record: dict[str, Any]) -> bool:
    observed = pair_control_record.get("observed")
    return (
        isinstance(observed, dict)
        and len(observed) == 2
        and all(
            isinstance(arm, dict) and arm.get("present") is True
            for arm in observed.values()
        )
    )


def _preflight_digest(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "rq3-preflight.v2"
        or payload.get("protocol_id") != "rq3-protocol.v2"
        or payload.get("status") != "PASS"
        or payload.get("planned_cycle_count") != 1
        or payload.get("completed_cycle_count") != 1
        or payload.get("passed_cycle_count") != 1
        or payload.get("topology_plan") != TOPOLOGY_PLANS["M3"].to_dict()
    ):
        raise ValueError(f"RQ3 topology preflight must pass its single topology rehearsal: {path}")
    return _sha256(path)


def _runtime_provenance(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "seed_base"}


def run_campaign(
    *,
    output_root: Path,
    repetitions: int = DEFAULT_REPETITIONS,
    seed_base: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    if not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        raise ValueError(
            f"repetitions must be between {MIN_REPETITIONS} and {MAX_REPETITIONS}"
        )
    configurations = load_configurations(ROOT / "configs/configurations.json")
    _validate_configuration_contrasts(configurations)
    metadata = campaign_runtime_metadata(output_root)
    _require_frozen_provenance("rq3", metadata)
    _, anchor_manifest_sha256 = verify_anchor_manifest(ROOT)
    preflight_path = ROOT / "results/raw/rq3-preflight.json"
    preflight_sha256 = _preflight_digest(preflight_path)
    actual_seed_base = seed_base if seed_base is not None else int(metadata["seed_base"]) + 100_000
    if actual_seed_base < 0:
        raise ValueError("seed base must be non-negative")
    cases = _plan(repetitions, actual_seed_base)
    manifest_path = output_root / "campaign-manifest.json"
    records: dict[str, dict[str, Any]] = {}
    started_ns = time.monotonic_ns()
    planned_cases = {case.trial_id: case for case in cases}

    if manifest_path.exists():
        if not resume:
            raise ValueError(f"{manifest_path} already exists; pass --resume to continue")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            previous.get("schema_version") != "rq3-campaign.v2"
            or previous.get("protocol_id") != "rq3-protocol.v2"
            or previous.get("repetitions_per_contrast") != repetitions
            or previous.get("seed_base") != actual_seed_base
            or previous.get("runner_script_sha256") != _sha256(Path(__file__))
            or previous.get("configuration_sha256") != _sha256(ROOT / "configs/configurations.json")
            or previous.get("protocol_sha256") != metadata.get("protocol_hash")
            or previous.get("preflight_sha256") != preflight_sha256
            or previous.get("anchor_manifest_sha256") != anchor_manifest_sha256
            or previous.get("topology_plan_sha256")
            != _sha256(ROOT / "src/mongo_consistency/rq3.py")
            or previous.get("runner_commit") != metadata.get("runner_commit")
            or previous.get("runtime_provenance") != _runtime_provenance(metadata)
            or previous.get("contrasts") != _contrast_rows(repetitions)
        ):
            raise ValueError("existing RQ3 campaign manifest has different frozen inputs, provenance, or plan")
        started_ns = int(previous.get("started_ns", started_ns))
        seen_trials: set[str] = set()
        for record in previous.get("records", []):
            if not isinstance(record, dict) or not isinstance(record.get("trial_id"), str):
                raise TypeError("existing RQ3 manifest contains an invalid case record")
            trial_id = record["trial_id"]
            case = planned_cases.get(trial_id)
            if case is None or trial_id in seen_trials:
                raise ValueError("existing RQ3 manifest contains an unknown or duplicate trial ID")
            seen_trials.add(trial_id)
            immutable_fields = {
                "ordinal": case.ordinal,
                "campaign": case.campaign,
                "contrast_id": case.contrast.contrast_id,
                "pair_id": case.pair_id,
                "replicate": case.replicate,
                "pair_seed": case.pair_seed,
                "configuration_id": case.configuration_id,
                "property": case.contrast.property_name,
                "seed": case.pair_seed,
                "adversarial": True,
            }
            if any(record.get(key) != value for key, value in immutable_fields.items()):
                raise ValueError(f"existing RQ3 record has changed plan fields: {trial_id}")
            records[record["trial_id"]] = record

    for case in cases:
        path = output_root / case.campaign / f"{case.trial_id}.json"
        if path.is_file():
            record = _record_for_history(case, path, metadata)
            prior = records.get(case.trial_id)
            if prior is not None and any(
                prior.get(key) != record.get(key) for key in record
            ):
                raise ValueError(f"history record changed since the RQ3 manifest was written: {path}")
            records[case.trial_id] = record
        elif case.trial_id in records:
            raise ValueError(f"RQ3 manifest points to a missing history: {path}")

    for case in cases:
        record = records.get(case.trial_id)
        if record is None:
            continue
        path = Path(str(record["path"]))
        if not path.is_absolute():
            path = ROOT / path
        history = read_history(path).to_dict()
        errors = arm_control_errors(
            history,
            case.contrast.contrast_id,
            case.configuration_id,
            configurations,
        )
        if errors:
            raise ValueError(
                f"cannot resume control-invalid history {case.trial_id}: "
                + "; ".join(errors)
            )
    for pair_entry in _pair_control_rows(cases, records, configurations):
        if _pair_is_complete(pair_entry) and pair_entry["control_valid"] is not True:
            raise ValueError(
                f"cannot resume control-invalid pair {pair_entry['pair_id']}: "
                + "; ".join(pair_entry["invalid_reasons"])
            )

    _write_json_atomic(
        manifest_path,
        _manifest_payload(
            status="RUNNING",
            repetitions=repetitions,
            seed_base=actual_seed_base,
            metadata=metadata,
            cases=cases,
            records=records,
            configurations=configurations,
            started_ns=started_ns,
            preflight_sha256=preflight_sha256,
            anchor_manifest_sha256=anchor_manifest_sha256,
        ),
    )
    seed_uris = tuple(
        value
        for value in os.environ.get(
            "MONGO_SEEDS",
            "mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0",
        ).split(";")
        if value
    )
    controller: FaultControllerClient = controller_from_environment()

    try:
        for case in cases:
            if case.trial_id in records:
                continue
            plan = TOPOLOGY_PLANS[case.contrast.contrast_id]
            with TopologyOracle() as oracle:
                normalization_state = normalize_topology(
                    oracle,
                    controller,
                    plan,
                    event_id=f"{case.trial_id}-normalize",
                )
            runtime_metadata = {
                **metadata,
                "seed_base": actual_seed_base,
                "rq3_protocol_id": "rq3-protocol.v2",
                "rq3_contrast_id": case.contrast.contrast_id,
                "rq3_mechanism": case.contrast.mechanism,
                "rq3_changed_factor": case.contrast.changed_factor,
                "rq3_pair_id": case.pair_id,
                "rq3_pair_seed": case.pair_seed,
                "rq3_replicate": case.replicate,
                "rq3_topology_plan": plan.to_dict(),
                "rq3_normalization": normalization_state,
                "topology_capture_operations": list(case.contrast.capture_operations),
                "topology_capture_policy": "direct-member-snapshots-before-and-after-selected-subject-operations.v2",
            }
            record = run_case(
                campaign=case.campaign,
                ordinal=case.ordinal,
                configuration=configurations[case.configuration_id],
                property_name=case.contrast.property_name,
                adversarial=True,
                seed_uris=seed_uris,
                controller=controller,
                output_root=output_root,
                runtime_metadata=runtime_metadata,
                seed_override=case.pair_seed,
            )
            record_path = Path(record["path"])
            if record_path.is_relative_to(ROOT):
                record["path"] = record_path.relative_to(ROOT).as_posix()
            records[case.trial_id] = {
                **record,
                "contrast_id": case.contrast.contrast_id,
                "pair_id": case.pair_id,
                "replicate": case.replicate,
                "pair_seed": case.pair_seed,
            }
            print(json.dumps(records[case.trial_id], sort_keys=True), flush=True)
            running = _manifest_payload(
                status="RUNNING",
                repetitions=repetitions,
                seed_base=actual_seed_base,
                metadata=metadata,
                cases=cases,
                records=records,
                configurations=configurations,
                started_ns=started_ns,
                preflight_sha256=preflight_sha256,
                anchor_manifest_sha256=anchor_manifest_sha256,
            )
            _write_json_atomic(manifest_path, running)
            history = read_history(Path(record["path"]) if Path(record["path"]).is_absolute() else ROOT / record["path"])
            arm_errors = arm_control_errors(
                history.to_dict(),
                case.contrast.contrast_id,
                case.configuration_id,
                configurations,
            )
            pair_entry = next(
                item
                for item in running["pair_controls"]
                if item["contrast_id"] == case.contrast.contrast_id
                and item["pair_id"] == case.pair_id
            )
            if arm_errors or (
                _pair_is_complete(pair_entry) and pair_entry["control_valid"] is not True
            ):
                reasons = arm_errors or pair_entry["invalid_reasons"]
                raise RuntimeError(
                    f"RQ3 control validation failed for {case.pair_id}/{case.configuration_id}: "
                    + "; ".join(reasons)
                )
    except Exception:
        _write_json_atomic(
            manifest_path,
            _manifest_payload(
                status="FAILED",
                repetitions=repetitions,
                seed_base=actual_seed_base,
                metadata=metadata,
                cases=cases,
                records=records,
                configurations=configurations,
                started_ns=started_ns,
                preflight_sha256=preflight_sha256,
                anchor_manifest_sha256=anchor_manifest_sha256,
                finished_ns=time.monotonic_ns(),
            ),
        )
        raise

    final = _manifest_payload(
        status="COMPLETE",
        repetitions=repetitions,
        seed_base=actual_seed_base,
        metadata=metadata,
        cases=cases,
        records=records,
        configurations=configurations,
        started_ns=started_ns,
        preflight_sha256=preflight_sha256,
        anchor_manifest_sha256=anchor_manifest_sha256,
        finished_ns=time.monotonic_ns(),
    )
    _write_json_atomic(manifest_path, final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-root", type=Path, default=ROOT / "results/raw/rq3-v2")
    args = parser.parse_args()
    manifest = run_campaign(
        output_root=args.output_root,
        repetitions=args.repetitions,
        seed_base=args.seed_base,
        resume=args.resume,
    )
    print(
        json.dumps(
            {
                "campaign": "rq3",
                "status": manifest["status"],
                "completed_case_count": manifest["completed_case_count"],
                "planned_case_count": manifest["planned_case_count"],
                "repetitions_per_contrast": manifest["repetitions_per_contrast"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if manifest["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
