"""Summarize matched RQ3 histories into JSON analysis artifacts.

The report source is authored in the repository. This analyzer writes only
machine-readable JSON used for audit and selection; it does not write LaTeX,
figures, or report prose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations
from mongo_consistency.history import read_history
from mongo_consistency.rq3 import pair_control
from mongo_consistency.rq3_anchors import verify_anchor_manifest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "results/raw/rq3"
DEFAULT_OUTPUT = ROOT / "results/analysis/rq3"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected an object in {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _history_path(record: dict[str, Any], *, input_root: Path | None = None) -> Path:
    value = Path(str(record["path"]))
    if value.is_absolute() and value.is_file():
        return value
    candidates: list[Path] = []
    if input_root is not None:
        candidates.extend(
            (input_root.parent.parent / value, input_root.parent / value, input_root / value)
        )
    candidates.append(ROOT / value)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def _operation(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return next(
        (
            operation
            for operation in history.get("operations", [])
            if operation.get("operation_id") == operation_id
        ),
        {},
    )


def _final_members(history: dict[str, Any]) -> dict[str, dict[str, Any]]:
    final = history.get("final_observation")
    members = final.get("members") if isinstance(final, dict) else None
    return members if isinstance(members, dict) else {}


def _final_write_presence(history: dict[str, Any], write_id: str) -> str:
    final = history.get("final_observation")
    topology = final.get("topology") if isinstance(final, dict) else None
    if (
        not isinstance(final, dict)
        or final.get("converged") is not True
        or not isinstance(topology, dict)
        or topology.get("stable") is not True
    ):
        return "unobserved"
    members = _final_members(history)
    if (
        set(members) != {"mongo1", "mongo2", "mongo3"}
        or any(not isinstance(member, dict) for member in members.values())
        or any(member.get("reachable") is not True for member in members.values())
    ):
        return "unobserved"
    present = [write_id in member.get("observed_write_ids", []) for member in members.values()]
    if all(present):
        return "all_members"
    if any(present):
        return "some_members"
    return "no_members"


def _final_contains_read_version(history: dict[str, Any]) -> bool:
    version = _read_signature(history)["version"]
    if not isinstance(version, int) or _final_write_presence(history, "w2") != "all_members":
        return False
    return all(
        version in member.get("observed_versions", [])
        for member in _final_members(history).values()
    )


def _operation_duration(operation: dict[str, Any]) -> float | None:
    events = operation.get("command_events", [])
    event = events[0] if isinstance(events, list) and events else {}
    start = event.get("started_ns", operation.get("start_ns"))
    end = event.get("end_ns", operation.get("end_ns"))
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return None
    return round((end - start) / 1_000_000, 2)


def _read_signature(history: dict[str, Any]) -> dict[str, Any]:
    operation = _operation(history, "read")
    return {
        "status": operation.get("operation_status"),
        "version": operation.get("observed_version"),
        "route": operation.get("actual_server_address"),
        "after_cluster_time": operation.get("after_cluster_time"),
        "error": operation.get("error_code"),
        "duration_ms": _operation_duration(operation),
        "topology_before": operation.get("topology_before"),
    }


def _write_signature(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    operation = _operation(history, operation_id)
    return {
        "status": operation.get("operation_status"),
        "route": operation.get("actual_server_address"),
        "error": operation.get("error_code"),
        "duration_ms": _operation_duration(operation),
    }


def _timestamp_relation(value: Any, boundary: Any) -> str:
    if not isinstance(value, dict) or not isinstance(boundary, dict):
        return "unknown"
    value_time = (value.get("seconds"), value.get("increment"))
    boundary_time = (boundary.get("seconds"), boundary.get("increment"))
    if not all(type(part) is int for part in (*value_time, *boundary_time)):
        return "unknown"
    if value_time > boundary_time:
        return "ahead"
    if value_time < boundary_time:
        return "behind"
    return "at"


def _member_from_address(address: str | None) -> str:
    return address.split(":", maxsplit=1)[0] if address else "unknown member"


def _route_timestamp_relation(history: dict[str, Any], operation_id: str, boundary: Any) -> str:
    operation = _operation(history, operation_id)
    topology = operation.get("topology_before")
    address = operation.get("actual_server_address")
    if (
        not isinstance(topology, dict)
        or topology.get("stable") is not True
        or not isinstance(address, str)
    ):
        return "unknown"
    members = topology.get("members")
    member = members.get(_member_from_address(address)) if isinstance(members, dict) else None
    if not isinstance(member, dict):
        return "unknown"
    return _timestamp_relation(boundary, member.get("last_write_op_time"))


def _same_actual_route(
    pairs: list[dict[str, Any]], operation_id: str, left_id: str, right_id: str
) -> int:
    matches = 0
    for pair in pairs:
        left = _operation(pair["arms"][left_id], operation_id).get("actual_server_address")
        right = _operation(pair["arms"][right_id], operation_id).get("actual_server_address")
        matches += int(isinstance(left, str) and bool(left) and left == right)
    return matches


def _error_code_counts(
    pairs: list[dict[str, Any]], configuration_id: str, operation_id: str
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        error_code = _operation(pair["arms"][configuration_id], operation_id).get("error_code")
        if error_code is not None:
            counts[str(error_code)] += 1
    return dict(sorted(counts.items()))


def _summarize_historical_anchors(
    anchor_manifest: dict[str, Any], *, repository_root: Path = ROOT
) -> list[dict[str, Any]]:
    operation_ids = {
        "M1": ("write", "read"),
        "M2": ("read", "write"),
        "M3": ("first_write", "second_write"),
    }
    pairs: list[dict[str, Any]] = []
    for pair in anchor_manifest["pairs"]:
        histories: list[dict[str, Any]] = []
        for record in pair["histories"]:
            history_object = read_history(repository_root / record["path"])
            history = history_object.to_dict()
            observed_operations: dict[str, Any] = {}
            for operation_id in operation_ids[pair["contrast_id"]]:
                operation = _operation(history, operation_id)
                if operation:
                    observed_operations[operation_id] = {
                        key: operation.get(key)
                        for key in (
                            "actual_server_address",
                            "actual_role",
                            "operation_status",
                            "observed_version",
                            "after_cluster_time",
                            "read_concern",
                            "write_concern",
                            "error_code",
                            "response_received",
                        )
                    }
                    observed_operations[operation_id]["duration_ms"] = _operation_duration(operation)
            final = history.get("final_observation", {})
            final_members = final.get("members", {}) if isinstance(final, dict) else {}
            histories.append(
                {
                    "trial_id": record["trial_id"],
                    "configuration_id": record["configuration_id"],
                    "seed": record["seed"],
                    "path": record["path"],
                    "raw_sha256": record["raw_sha256"],
                    "history_hash": record["history_hash"],
                    "outcome": check_history(history_object).outcome.value,
                    "operations": observed_operations,
                    "final_observation": {
                        "converged": final.get("converged") if isinstance(final, dict) else None,
                        "primary": final.get("topology", {}).get("primary")
                        if isinstance(final, dict) and isinstance(final.get("topology"), dict)
                        else None,
                        "members": {
                            member: {
                                "reachable": state.get("reachable"),
                                "observed_versions": state.get("observed_versions"),
                                "observed_write_ids": state.get("observed_write_ids"),
                            }
                            for member, state in final_members.items()
                            if isinstance(state, dict)
                        },
                    },
                }
            )
        pairs.append(
            {
                "contrast_id": pair["contrast_id"],
                "property": pair["property"],
                "interpretation": pair["interpretation"],
                "limitation": pair["limitation"],
                "histories": histories,
            }
        )
    return pairs


def _row_for_pair(
    contrast_id: str,
    pair_id: str,
    arms: dict[str, dict[str, Any]],
    configurations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    controls = pair_control(contrast_id, pair_id, arms, configurations=configurations)
    return {
        "pair_id": pair_id,
        "arms": arms,
        "control_valid": controls["control_valid"],
        "invalid_reasons": controls["invalid_reasons"],
        "controls": controls,
    }


def _valid_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pair for pair in pairs if pair.get("control_valid") is True]


def _history_id(history: dict[str, Any]) -> str:
    return str(history.get("manifest", {}).get("trial_id", "unknown"))


def _counts(pairs: list[dict[str, Any]], contrast_id: str) -> dict[str, Any]:
    if contrast_id == "M1":
        return {
            "C5_stale_success_without_after_cluster_time": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                and _read_signature(pair["arms"]["C5"])["after_cluster_time"] is None
                for pair in pairs
            ),
            "C5_write_time_ahead_of_routed_member_last_write": sum(
                _route_timestamp_relation(
                    pair["arms"]["C5"],
                    "read",
                    _operation(pair["arms"]["C5"], "write").get("operation_time_after"),
                )
                == "ahead"
                for pair in pairs
            ),
            "C6_after_cluster_time_present": sum(
                _read_signature(pair["arms"]["C6"])["after_cluster_time"] is not None
                for pair in pairs
            ),
            "C6_after_cluster_time_matches_write_time": sum(
                _read_signature(pair["arms"]["C6"])["after_cluster_time"]
                == _operation(pair["arms"]["C6"], "write").get("operation_time_after")
                and _read_signature(pair["arms"]["C6"])["after_cluster_time"] is not None
                for pair in pairs
            ),
            "C6_after_cluster_time_ahead_of_routed_member_last_write": sum(
                _route_timestamp_relation(
                    pair["arms"]["C6"],
                    "read",
                    _read_signature(pair["arms"]["C6"])["after_cluster_time"],
                )
                == "ahead"
                for pair in pairs
            ),
            "C6_read_attempted": sum(
                _read_signature(pair["arms"]["C6"])["status"] is not None for pair in pairs
            ),
            "C6_unavailable_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "UNAVAILABLE" for pair in pairs
            ),
            "C6_indeterminate_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "INDETERMINATE" for pair in pairs
            ),
            "C6_read_error_code_counts": _error_code_counts(pairs, "C6", "read"),
            "same_actual_read_route_pairs": _same_actual_route(pairs, "read", "C5", "C6"),
            "C6_successful_read_responses": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS" for pair in pairs
            ),
            "C6_successful_reads_without_version": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and not isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                for pair in pairs
            ),
            "C6_nonstale_successful_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                and _read_signature(pair["arms"]["C6"])["version"] >= 1
                for pair in pairs
            ),
            "C6_stale_successful_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                and isinstance(_read_signature(pair["arms"]["C6"])["version"], int)
                and _read_signature(pair["arms"]["C6"])["version"] < 1
                for pair in pairs
            ),
            "C6_read_not_reached": sum(
                _read_signature(pair["arms"]["C6"])["status"] is None for pair in pairs
            ),
        }
    if contrast_id == "M2":
        return {
            "setup_w1_command_w1_mongo3_arms": sum(
                isinstance(
                    setup := pair["controls"]["observed"][configuration_id]["observed"].get("setup_write"),
                    dict,
                )
                and setup.get("write_concern", {}).get("w") == 1
                and setup.get("member") == "mongo3"
                for pair in pairs
                for configuration_id in ("C8", "C5")
            ),
            "C8_local_read_v1": sum(
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C8"])["version"] == 1
                for pair in pairs
            ),
            "C8_successful_read_responses": sum(
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS" for pair in pairs
            ),
            "C5_majority_read_v0": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                for pair in pairs
            ),
            "C5_successful_read_responses": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS" for pair in pairs
            ),
            "same_actual_read_route_pairs": _same_actual_route(pairs, "read", "C8", "C5"),
            "C8_W2_acknowledged": sum(
                _operation(pair["arms"]["C8"], "write").get("operation_status") == "SUCCESS"
                for pair in pairs
            ),
            "C8_W2_non_success": sum(
                _operation(pair["arms"]["C8"], "write").get("operation_status")
                in {"UNAVAILABLE", "INDETERMINATE"}
                for pair in pairs
            ),
            "C5_W2_acknowledged": sum(
                _operation(pair["arms"]["C5"], "write").get("operation_status") == "SUCCESS"
                for pair in pairs
            ),
            "C5_W2_non_success": sum(
                _operation(pair["arms"]["C5"], "write").get("operation_status")
                in {"UNAVAILABLE", "INDETERMINATE"}
                for pair in pairs
            ),
            "C8_W2_present_on_all_final_members": sum(
                _final_write_presence(pair["arms"]["C8"], "w2") == "all_members" for pair in pairs
            ),
            "C5_W2_present_on_all_final_members": sum(
                _final_write_presence(pair["arms"]["C5"], "w2") == "all_members" for pair in pairs
            ),
            "C8_read_version_present_in_final_state": sum(
                _final_contains_read_version(pair["arms"]["C8"])
                for pair in pairs
            ),
            "C5_read_version_present_in_final_state": sum(
                _final_contains_read_version(pair["arms"]["C5"])
                for pair in pairs
            ),
            "C8_WFR_violations": sum(pair["arms"]["C8"].get("result") == "VIOLATION" for pair in pairs),
            "C8_WFR_indeterminate": sum(pair["arms"]["C8"].get("result") == "INDETERMINATE" for pair in pairs),
            "C5_WFR_passes": sum(pair["arms"]["C5"].get("result") == "PASS" for pair in pairs),
            "C5_WFR_indeterminate": sum(pair["arms"]["C5"].get("result") == "INDETERMINATE" for pair in pairs),
        }
    return {
        "C3_w1_first_write_acknowledged": sum(
            _write_signature(pair["arms"]["C3"], "first_write")["status"] == "SUCCESS"
            for pair in pairs
        ),
        "C3_acknowledged_w1_absent_from_all_converged_members": sum(
            _write_signature(pair["arms"]["C3"], "first_write")["status"] == "SUCCESS"
            and _final_write_presence(pair["arms"]["C3"], "w1") == "no_members"
            for pair in pairs
        ),
        "C3_converged_final_observations": sum(
            _final_write_presence(pair["arms"]["C3"], "w1") != "unobserved" for pair in pairs
        ),
        "C6_majority_first_write_acknowledged": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["status"] == "SUCCESS"
            for pair in pairs
        ),
        "C6_majority_first_write_network_timeout": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["error"] == "NetworkTimeout"
            for pair in pairs
        ),
        "C6_w1_present_on_all_final_members_after_timeout": sum(
            _write_signature(pair["arms"]["C6"], "first_write")["error"] == "NetworkTimeout"
            and _final_write_presence(pair["arms"]["C6"], "w1") == "all_members"
            for pair in pairs
        ),
        "same_actual_first_write_route_pairs": _same_actual_route(
            pairs, "first_write", "C3", "C6"
        ),
        "C6_converged_final_observations": sum(
            _final_write_presence(pair["arms"]["C6"], "w1") != "unobserved" for pair in pairs
        ),
    }


def _summary_and_report(*, input_root: Path, output_root: Path) -> dict[str, Any]:
    manifest_path = input_root / "campaign-manifest.json"
    campaign = _read_json(manifest_path)
    anchor_manifest, anchor_manifest_digest = verify_anchor_manifest(ROOT)
    historical_anchors = _summarize_historical_anchors(anchor_manifest)
    if campaign.get("schema_version") != "rq3-campaign.v2":
        raise ValueError("RQ3 analyzer requires an rq3-campaign.v2 campaign")
    if campaign.get("protocol_id") != "rq3-protocol.v2":
        raise ValueError("RQ3 analyzer requires the rq3-protocol.v2 protocol")
    if campaign.get("status") != "COMPLETE":
        raise ValueError(f"RQ3 campaign is not complete: {campaign.get('status')}")
    repetitions = campaign.get("repetitions_per_contrast")
    if type(repetitions) is not int or repetitions != 8:
        raise ValueError("RQ3 campaign repetitions must be exactly eight")
    records = campaign.get("records")
    if not isinstance(records, list) or len(records) != repetitions * 6:
        raise ValueError("RQ3 campaign manifest does not contain every planned history")

    configurations = load_configurations(ROOT / "configs/configurations.json")
    contrast_specs = {
        "M1": ("rq3-m1", {"C5", "C6"}, "RYW", "m1"),
        "M2": ("rq3-m2", {"C8", "C5"}, "WFR", "m2"),
        "M3": ("rq3-m3", {"C3", "C6"}, "MW", "m3"),
    }
    records_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    seen_trials: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("RQ3 campaign contains a non-object record")
        contrast_id = record.get("contrast_id")
        spec = contrast_specs.get(contrast_id) if isinstance(contrast_id, str) else None
        if spec is None:
            raise ValueError(f"unknown RQ3 contrast: {contrast_id!r}")
        campaign_id, expected_arms, property_name, pair_prefix = spec
        configuration_id = record.get("configuration_id")
        pair_id = record.get("pair_id")
        replicate = record.get("replicate")
        trial_id = record.get("trial_id")
        pair_seed = record.get("pair_seed")
        if (
            record.get("campaign") != campaign_id
            or configuration_id not in expected_arms
            or record.get("property") != property_name
            or type(replicate) is not int
            or not 1 <= replicate <= repetitions
            or pair_id != f"{pair_prefix}-r{replicate:02d}"
            or type(pair_seed) is not int
            or record.get("seed") != pair_seed
            or not isinstance(trial_id, str)
            or not trial_id
            or trial_id in seen_trials
        ):
            raise ValueError(f"invalid RQ3 matched-pair identity for {trial_id!r}")
        seen_trials.add(trial_id)
        path = _history_path(record, input_root=input_root)
        history_object = read_history(path)
        if history_object.history_hash != record.get("history_hash"):
            raise ValueError(f"history hash differs from the RQ3 campaign manifest: {path}")
        history = history_object.to_dict()
        history_manifest = history.get("manifest", {})
        for key, expected in {
            "trial_id": trial_id,
            "configuration_id": configuration_id,
            "property": property_name,
            "seed": pair_seed,
            "rq3_contrast_id": contrast_id,
            "rq3_pair_id": pair_id,
            "rq3_pair_seed": pair_seed,
            "rq3_replicate": replicate,
            "rq3_protocol_id": "rq3-protocol.v2",
        }.items():
            if history_manifest.get(key) != expected:
                raise ValueError(f"history identity differs from its campaign record: {path}")
        history["result"] = record.get("outcome")
        history["source_path"] = _display_path(path)
        if configuration_id in records_by_pair[pair_id]:
            raise ValueError(f"{pair_id} contains a duplicate configuration arm")
        records_by_pair[pair_id][configuration_id] = history

    expected_pair_ids = {
        f"{contrast_id.lower()}-r{replicate:02d}"
        for contrast_id in contrast_specs
        for replicate in range(1, repetitions + 1)
    }
    if set(records_by_pair) != expected_pair_ids:
        raise ValueError("RQ3 campaign has missing or unexpected pair IDs")

    grouped: dict[str, list[dict[str, Any]]] = {"M1": [], "M2": [], "M3": []}
    for pair_id, arms in records_by_pair.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        if set(arms) != contrast_specs[contrast_id][1]:
            raise ValueError(f"{pair_id} does not contain the registered configuration pair")
        grouped[contrast_id].append(_row_for_pair(contrast_id, pair_id, arms, configurations))

    selections: dict[str, Any] = {}
    summary_contrasts: dict[str, Any] = {}
    configuration_order = {"M1": ("C5", "C6"), "M2": ("C8", "C5"), "M3": ("C3", "C6")}
    for contrast_id, pairs in grouped.items():
        pairs.sort(key=lambda pair: pair["pair_id"])
        valid_pairs = _valid_pairs(pairs)
        chosen = valid_pairs[0] if valid_pairs else None
        selection = None
        if chosen is not None:
            selection = {
                "pair_id": chosen["pair_id"],
                "control_valid": True,
                "histories": [
                    {
                        "trial_id": _history_id(chosen["arms"][configuration_id]),
                        "path": chosen["arms"][configuration_id]["source_path"],
                        "sha256": chosen["arms"][configuration_id]["history_hash"],
                        "configuration_id": configuration_id,
                    }
                    for configuration_id in configuration_order[contrast_id]
                ],
            }
        selections[contrast_id] = selection
        invalid_pairs = [
            {"pair_id": pair["pair_id"], "invalid_reasons": pair["invalid_reasons"]}
            for pair in pairs
            if not pair["control_valid"]
        ]
        summary_contrasts[contrast_id] = {
            "planned_pair_count": len(pairs),
            "control_valid_pair_count": len(valid_pairs),
            "invalid_pair_count": len(invalid_pairs),
            "invalid_pairs": invalid_pairs,
            "signature_counts": _counts(valid_pairs, contrast_id),
            "selected_pair": selection,
        }

    manifest_digest = _sha256(manifest_path)
    output_root.mkdir(parents=True, exist_ok=True)
    selection_manifest = {
        "schema_version": "rq3-selection.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": _display_path(manifest_path),
        "campaign_manifest_sha256": manifest_digest,
        "preflight_sha256": campaign["preflight_sha256"],
        "anchor_manifest_sha256": anchor_manifest_digest,
        "anchor_history_ids": [
            history["trial_id"]
            for pair in historical_anchors
            for history in pair["histories"]
        ],
        "selected_pairs": selections,
    }
    selection_path = output_root / "selection-manifest.json"
    selection_path.write_text(
        json.dumps(selection_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary = {
        "schema_version": "rq3-analysis.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": _display_path(manifest_path),
        "campaign_manifest_sha256": manifest_digest,
        "preflight_sha256": campaign["preflight_sha256"],
        "anchor_manifest_sha256": anchor_manifest_digest,
        "historical_anchors": historical_anchors,
        "repetitions_per_contrast": repetitions,
        "contrast_summaries": summary_contrasts,
        "selection_manifest": _display_path(selection_path),
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    summary = _summary_and_report(input_root=args.input_root, output_root=args.output_root)
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
