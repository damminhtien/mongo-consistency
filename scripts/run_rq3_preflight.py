"""Verify deterministic RQ3 topology normalization before the scientific campaign."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from run_campaign import (
    _require_frozen_provenance,
    _write_json_atomic,
    campaign_runtime_metadata,
    controller_from_environment,
)

from mongo_consistency.rq3 import TOPOLOGY_PLANS, normalize_topology
from mongo_consistency.topology import TopologyError, TopologyOracle

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CYCLES = 10
OUTPUT = ROOT / "results/raw/rq3-preflight.json"


def run_preflight(*, output: Path = OUTPUT, cycles: int = DEFAULT_CYCLES) -> dict[str, Any]:
    if cycles != DEFAULT_CYCLES:
        raise ValueError("protocol v2 requires exactly ten topology rehearsals")
    plan = TOPOLOGY_PLANS["M3"]
    controller = controller_from_environment()
    metadata = campaign_runtime_metadata(ROOT / "results/raw")
    _require_frozen_provenance("rq3-preflight", metadata)
    started_ns = time.monotonic_ns()
    completed: list[dict[str, Any]] = []
    error: str | None = None

    for cycle in range(1, cycles + 1):
        cycle_id = f"rq3-preflight-v2-c{cycle:02d}"
        row: dict[str, Any] = {"cycle": cycle, "cycle_id": cycle_id}
        try:
            with TopologyOracle() as oracle:
                initial = normalize_topology(
                    oracle,
                    controller,
                    plan,
                    event_id=f"{cycle_id}-initial",
                )
                initial_roles = {
                    member: state["role"]
                    for member, state in initial["members"].items()
                }
                if initial["primary"] != "mongo3" or initial_roles != {
                    "mongo1": "SECONDARY",
                    "mongo2": "SECONDARY",
                    "mongo3": "PRIMARY",
                }:
                    raise TopologyError(f"initial direct roles differ from the plan: {initial_roles}")

                guard_state = oracle.member_state("mongo1")
                if guard_state.get("reachable") is not True or guard_state.get("role") != "SECONDARY":
                    raise TopologyError(f"mongo1 cannot serve as the election guard: {guard_state}")
                oracle.freeze_member("mongo1", 120)
                isolation_id = f"{cycle_id}-partition"
                isolation = controller.isolate("mongo3", isolation_id)
                health = controller.health()
                if health.get("mongo3", {}).get("replication_isolated") is not True:
                    raise TopologyError(f"mongo3 isolation was not verified: {health}")

                new_primary = oracle.wait_for_majority_primary(
                    "mongo3",
                    timeout_seconds=45.0,
                    expected_primary="mongo2",
                    stable_samples=3,
                )
                majority_roles = {
                    member: oracle.member_state(member)
                    for member in ("mongo1", "mongo2")
                }
                isolated_state = oracle.member_state("mongo3")
                if (
                    new_primary != "mongo2"
                    or majority_roles["mongo2"].get("role") != "PRIMARY"
                    or majority_roles["mongo1"].get("role") != "SECONDARY"
                    or isolated_state.get("reachable") is not True
                ):
                    raise TopologyError(
                        "partition roles do not prove mongo2 PRIMARY, mongo1 SECONDARY, "
                        f"and reachable mongo3: {majority_roles!r}, {isolated_state!r}"
                    )

                recovery = normalize_topology(
                    oracle,
                    controller,
                    plan,
                    event_id=f"{cycle_id}-recovery",
                )
                recovery_roles = {
                    member: state["role"]
                    for member, state in recovery["members"].items()
                }
                if recovery["primary"] != "mongo3" or recovery_roles != {
                    "mongo1": "SECONDARY",
                    "mongo2": "SECONDARY",
                    "mongo3": "PRIMARY",
                }:
                    raise TopologyError(f"recovery roles differ from the plan: {recovery_roles}")

                row.update(
                    {
                        "status": "PASS",
                        "initial": initial,
                        "guard_before_partition": guard_state,
                        "partition": {
                            "isolation": isolation,
                            "controller_health": health,
                            "majority_primary": new_primary,
                            "majority_roles": majority_roles,
                            "isolated_member": isolated_state,
                        },
                        "recovery": recovery,
                    }
                )
        except Exception as exception:  # noqa: BLE001 - preserve every failed cycle as evidence.
            row.update({"status": "FAIL", "error_type": type(exception).__name__, "error": str(exception)})
            error = f"cycle {cycle} failed: {type(exception).__name__}: {exception}"
        completed.append(row)
        payload = _payload(plan.to_dict(), metadata, completed, cycles, started_ns, error)
        _write_json_atomic(output, payload)
        if error is not None:
            break

    final = _payload(
        plan.to_dict(), metadata, completed, cycles, started_ns, error, finished=True
    )
    _write_json_atomic(output, final)
    return final


def _payload(
    plan: dict[str, str | None],
    metadata: dict[str, Any],
    completed: list[dict[str, Any]],
    planned: int,
    started_ns: int,
    error: str | None,
    *,
    finished: bool = False,
) -> dict[str, Any]:
    passed = sum(cycle.get("status") == "PASS" for cycle in completed)
    return {
        "schema_version": "rq3-preflight.v2",
        "protocol_id": "rq3-protocol.v2",
        "status": "PASS" if error is None and passed == planned else "FAIL",
        "topology_plan": plan,
        "planned_cycle_count": planned,
        "completed_cycle_count": len(completed),
        "passed_cycle_count": passed,
        "cycles": completed,
        "runtime_provenance": {
            key: value for key, value in metadata.items() if key != "seed_base"
        },
        "started_ns": started_ns,
        "last_updated_ns": time.monotonic_ns(),
        **({"error": error} if error is not None else {}),
        **({"finished_ns": time.monotonic_ns()} if finished else {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = run_preflight(output=args.output, cycles=args.cycles)
    print(
        json.dumps(
            {
                "status": result["status"],
                "passed_cycle_count": result["passed_cycle_count"],
                "planned_cycle_count": result["planned_cycle_count"],
                "output": str(args.output),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
