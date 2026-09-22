"""Summarize matched RQ3 histories and build the report table and timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from mongo_consistency.checkers import check_history
from mongo_consistency.config import load_configurations
from mongo_consistency.history import read_history
from mongo_consistency.rq3 import TOPOLOGY_PLANS, pair_control
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


def _history_path(record: dict[str, Any]) -> Path:
    value = Path(str(record["path"]))
    if value.is_absolute() and value.is_file():
        return value
    return ROOT / value


def _operation(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return next(
        (
            operation
            for operation in history.get("operations", [])
            if operation.get("operation_id") == operation_id
        ),
        {},
    )


def _initial_topology(history: dict[str, Any]) -> dict[str, Any]:
    checks = history.get("precondition", {}).get("checks", [])
    for check in checks:
        if check.get("name") == "one-primary-two-secondary-topology":
            actual = check.get("actual")
            return actual if isinstance(actual, dict) else {}
    return {}


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
        or any(
            member.get("reachable") is not True for member in members.values()
        )
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


def _duration_ms(operation: dict[str, Any]) -> float | None:
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
        "duration_ms": _duration_ms(operation),
        "topology_before": operation.get("topology_before"),
        "topology_after": operation.get("topology_after"),
    }


def _write_signature(history: dict[str, Any], operation_id: str) -> dict[str, Any]:
    operation = _operation(history, operation_id)
    return {
        "status": operation.get("operation_status"),
        "route": operation.get("actual_server_address"),
        "error": operation.get("error_code"),
        "duration_ms": _duration_ms(operation),
        "topology_before": operation.get("topology_before"),
        "topology_after": operation.get("topology_after"),
    }


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
        histories = []
        for record in pair["histories"]:
            history_object = read_history(repository_root / record["path"])
            history = history_object.to_dict()
            observed_operations = {}
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
                    observed_operations[operation_id]["duration_ms"] = _duration_ms(operation)
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
    controls = pair_control(
        contrast_id,
        pair_id,
        arms,
        configurations=configurations,
    )
    row = {
        "pair_id": pair_id,
        "arms": arms,
        "control_valid": controls["control_valid"],
        "invalid_reasons": controls["invalid_reasons"],
        "controls": controls,
    }
    return row


def _valid_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pair for pair in pairs if pair.get("control_valid") is True]


def _latex_escape(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in text)


def _history_id(history: dict[str, Any]) -> str:
    return str(history.get("manifest", {}).get("trial_id", "unknown"))


def _member_from_address(address: str | None) -> str:
    return address.split(":", maxsplit=1)[0] if address else "unknown member"


def _same_actual_route(
    pairs: list[dict[str, Any]], operation_id: str, left_id: str, right_id: str
) -> int:
    matches = 0
    for pair in pairs:
        left = _operation(pair["arms"][left_id], operation_id).get("actual_server_address")
        right = _operation(pair["arms"][right_id], operation_id).get("actual_server_address")
        matches += int(isinstance(left, str) and bool(left) and left == right)
    return matches


def _route_timestamp_relation(
    history: dict[str, Any], operation_id: str, boundary: Any
) -> str:
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
    route = _member_from_address(address)
    member = members.get(route) if isinstance(members, dict) else None
    if not isinstance(member, dict):
        return "unknown"
    return _timestamp_relation(boundary, member.get("last_write_op_time"))


def _error_code_counts(
    pairs: list[dict[str, Any]], configuration_id: str, operation_id: str
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        error_code = _operation(pair["arms"][configuration_id], operation_id).get("error_code")
        if error_code is not None:
            counts[str(error_code)] += 1
    return dict(sorted(counts.items()))


def _format_timestamp(value: Any) -> str:
    if isinstance(value, dict):
        seconds = value.get("seconds")
        increment = value.get("increment")
        if isinstance(seconds, int) and isinstance(increment, int):
            return f"{seconds}:{increment}"
        cluster_time = value.get("cluster_time")
        if isinstance(cluster_time, dict):
            return _format_timestamp(cluster_time)
    return "unavailable"


def _read_text(history: dict[str, Any]) -> str:
    read = _read_signature(history)
    operation = _operation(history, "read")
    events = operation.get("command_events", [])
    command_event = events[0] if isinstance(events, list) and events else {}
    time_bound = read["after_cluster_time"]
    bound_text = (
        "afterClusterTime absent"
        if time_bound is None
        else f"afterClusterTime={_format_timestamp(time_bound)}"
    )
    elapsed = "unavailable" if read["duration_ms"] is None else f"{read['duration_ms']} ms"
    op_time = _format_timestamp(operation.get("operation_time_before"))
    cluster_time = _format_timestamp(operation.get("cluster_time_before"))
    return (
        f"{read['route']}; {command_event.get('command_name')}; "
        f"RC={operation.get('read_concern')}; {bound_text}; "
        f"session opTime={op_time}, clusterTime={cluster_time}; command {elapsed}"
    )


def _read_result_text(history: dict[str, Any]) -> str:
    read = _read_signature(history)
    if read["status"] == "SUCCESS":
        result = f"SUCCESS, v{read['version']}"
    elif read["status"] is None:
        result = "read not reached"
    else:
        result = f"{read['status']}/{read['error'] or 'no error code'}"
    elapsed = "latency unavailable" if read["duration_ms"] is None else f"{read['duration_ms']} ms"
    return f"{result}, {elapsed}"


def _find_pdflatex() -> str | None:
    found = shutil.which("pdflatex")
    if found:
        return found
    candidates: list[Path] = []
    configured = os.environ.get("TEXLIVE_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    user_texlive = Path.home() / "texlive"
    if user_texlive.is_dir():
        for version in sorted(user_texlive.iterdir()):
            bin_root = version / "bin"
            if bin_root.is_dir():
                candidates.extend(path for path in sorted(bin_root.iterdir()) if path.is_dir())
    for directory in candidates:
        candidate = directory / "pdflatex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _snapshot_text(history: dict[str, Any], stage: str) -> str:
    read = _read_signature(history)
    snapshot = read.get(f"topology_{stage}")
    if not isinstance(snapshot, dict):
        return f"Topology {stage}: unavailable"
    target = _member_from_address(read["route"])
    members = snapshot.get("members")
    state = members.get(target, {}) if isinstance(members, dict) else {}
    term = snapshot.get("term")
    return (
        f"primary={snapshot.get('primary', 'unavailable')}; term="
        f"{'unavailable' if term is None else term}; {target} "
        f"role={state.get('role', 'unavailable')}; "
        f"lastWrite={_format_timestamp(state.get('last_write_op_time'))}; "
        f"majorityCommit={_format_timestamp(snapshot.get('last_committed_op_time'))}"
    )


def _write_trace_text(history: dict[str, Any]) -> str:
    operation = _operation(history, "write")
    write = _write_signature(history, "write")
    return (
        f"{write['status']} at {write['route']}; WC={operation.get('write_concern')}; "
        f"opTime={_format_timestamp(operation.get('operation_time_after'))}; "
        f"clusterTime={_format_timestamp(operation.get('cluster_time_after'))}"
    )


def _read_trace_text(history: dict[str, Any]) -> str:
    operation = _operation(history, "read")
    read = _read_signature(history)
    if read["status"] == "SUCCESS":
        result = f"SUCCESS v{read['version']}"
    elif read["status"] is None:
        result = "not reached"
    else:
        result = f"{read['status']}/{read['error'] or 'error unavailable'}"
    return (
        f"{result} at {read['route']}; RC={operation.get('read_concern')}; "
        f"afterClusterTime={_format_timestamp(read['after_cluster_time']) if read['after_cluster_time'] is not None else 'absent'}; "
        f"session opTime={_format_timestamp(operation.get('operation_time_before'))}, "
        f"clusterTime={_format_timestamp(operation.get('cluster_time_before'))}; "
        f"command={read['duration_ms'] if read['duration_ms'] is not None else 'unavailable'} ms"
    )


def _render_timeline(pair: dict[str, Any], destination: Path) -> None:
    arms = pair["arms"]
    c5 = arms["C5"]
    c6 = arms["C6"]
    c5_w1 = _final_write_presence(c5, "w1")
    c6_w1 = _final_write_presence(c6, "w1")
    c5_final = f"{_read_result_text(c5)}; W1 after heal={c5_w1}"
    c6_final = f"{_read_result_text(c6)}; W1 after heal={c6_w1}"
    initial_primary = _initial_topology(c5).get("primary", "unavailable")
    source = rf"""\documentclass[10pt]{{article}}
\usepackage[paperwidth=210mm,paperheight=155mm,margin=12mm]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{booktabs}}
\usepackage{{tabularx}}
\usepackage{{array}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}
\renewcommand{{\arraystretch}}{{1.35}}
\pdfinfoomitdate=1
\pdftrailerid{{}}
\begin{{document}}
\begin{{center}}
{{\Large\bfseries Causal-session contrast}}\\[0.25em]
{{\normalsize Matched seed; independent fault episodes; initial primary {initial_primary}}}\\[0.5em]
{{\footnotesize\texttt{{{_latex_escape(_history_id(c5))}}}\\
\texttt{{{_latex_escape(_history_id(c6))}}}}}
\end{{center}}
\vspace{{0.8em}}
\normalsize
\begin{{tabularx}}{{\textwidth}}{{@{{}}>{{\bfseries\raggedright\arraybackslash}}p{{0.16\textwidth}}>{{\raggedright\arraybackslash}}X>{{\raggedright\arraybackslash}}X@{{}}}}
\toprule
Stage & C5: causal session off & C6: causal session on \\
\midrule
1. W1 write & {_latex_escape(_write_trace_text(c5))} & {_latex_escape(_write_trace_text(c6))} \\
2. R1 read & {_latex_escape(_read_trace_text(c5))} & {_latex_escape(_read_trace_text(c6))} \\
3. Topology before R1 & {_latex_escape(_snapshot_text(c5, 'before'))} & {_latex_escape(_snapshot_text(c6, 'before'))} \\
4. Topology after R1 & {_latex_escape(_snapshot_text(c5, 'after'))} & {_latex_escape(_snapshot_text(c6, 'after'))} \\
5. Client result & {_latex_escape(c5_final)} & {_latex_escape(c6_final)} \\
\bottomrule
\end{{tabularx}}
\end{{document}}
"""
    with tempfile.TemporaryDirectory(prefix="rq3-timeline-") as temporary:
        build_dir = Path(temporary)
        source_path = build_dir / "rq3-causal-timeline.tex"
        source_path.write_text(source, encoding="utf-8")
        pdflatex = _find_pdflatex()
        if pdflatex is None:
            raise RuntimeError("pdflatex was not found on PATH, in TEXLIVE_BIN, or under ~/texlive")
        environment = os.environ.copy()
        executable_dir = str(Path(pdflatex).resolve().parent)
        path_entries = environment.get("PATH", "").split(os.pathsep)
        if executable_dir not in path_entries:
            environment["PATH"] = os.pathsep.join(
                (executable_dir, environment.get("PATH", ""))
            )
        result = subprocess.run(
            [
                pdflatex,
                "-halt-on-error",
                "-interaction=nonstopmode",
                "-output-directory",
                str(build_dir),
                str(source_path),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        pdf_path = build_dir / "rq3-causal-timeline.pdf"
        if result.returncode != 0 or not pdf_path.is_file():
            tail = (result.stdout + "\n" + result.stderr)[-5000:]
            raise RuntimeError(f"could not render the RQ3 timeline PDF:\n{tail}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, destination)


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
                _read_signature(pair["arms"]["C6"])["status"] is not None
                for pair in pairs
            ),
            "C6_unavailable_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "UNAVAILABLE"
                for pair in pairs
            ),
            "C6_indeterminate_reads": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "INDETERMINATE"
                for pair in pairs
            ),
            "C6_read_error_code_counts": _error_code_counts(pairs, "C6", "read"),
            "same_actual_read_route_pairs": _same_actual_route(pairs, "read", "C5", "C6"),
            "C6_successful_read_responses": sum(
                _read_signature(pair["arms"]["C6"])["status"] == "SUCCESS"
                for pair in pairs
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
                _read_signature(pair["arms"]["C6"])["status"] is None
                for pair in pairs
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
                _read_signature(pair["arms"]["C8"])["status"] == "SUCCESS"
                for pair in pairs
            ),
            "C5_majority_read_v0": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                and _read_signature(pair["arms"]["C5"])["version"] == 0
                for pair in pairs
            ),
            "C5_successful_read_responses": sum(
                _read_signature(pair["arms"]["C5"])["status"] == "SUCCESS"
                for pair in pairs
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
                _final_write_presence(pair["arms"]["C8"], "w2") == "all_members"
                for pair in pairs
            ),
            "C5_W2_present_on_all_final_members": sum(
                _final_write_presence(pair["arms"]["C5"], "w2") == "all_members"
                for pair in pairs
            ),
            "C8_read_version_present_in_final_state": sum(
                _final_contains_read_version(pair["arms"]["C8"])
                for pair in pairs
            ),
            "C5_read_version_present_in_final_state": sum(
                _final_contains_read_version(pair["arms"]["C5"])
                for pair in pairs
            ),
            "C8_WFR_violations": sum(
                pair["arms"]["C8"].get("result") == "VIOLATION" for pair in pairs
            ),
            "C8_WFR_indeterminate": sum(
                pair["arms"]["C8"].get("result") == "INDETERMINATE" for pair in pairs
            ),
            "C5_WFR_passes": sum(
                pair["arms"]["C5"].get("result") == "PASS" for pair in pairs
            ),
            "C5_WFR_indeterminate": sum(
                pair["arms"]["C5"].get("result") == "INDETERMINATE" for pair in pairs
            ),
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
            _final_write_presence(pair["arms"]["C3"], "w1") != "unobserved"
            for pair in pairs
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
            _final_write_presence(pair["arms"]["C6"], "w1") != "unobserved"
            for pair in pairs
        ),
    }


def _summary_and_report(
    *, input_root: Path, output_root: Path, submission_root: Path
) -> dict[str, Any]:
    manifest_path = input_root / "campaign-manifest.json"
    campaign = _read_json(manifest_path)
    anchor_manifest, anchor_manifest_digest = verify_anchor_manifest(ROOT)
    historical_anchors = _summarize_historical_anchors(anchor_manifest)
    if (
        campaign.get("schema_version") != "rq3-campaign.v2"
        or campaign.get("protocol_id") != "rq3-protocol.v2"
    ):
        raise ValueError("RQ3 analyzer requires an rq3-protocol.v2 campaign")
    if campaign.get("status") != "COMPLETE":
        raise ValueError(f"RQ3 campaign is not complete: {campaign.get('status')}")
    provenance = campaign.get("runtime_provenance")
    if not isinstance(provenance, dict):
        raise TypeError("RQ3 campaign runtime provenance is missing")
    configuration_settings = load_configurations(ROOT / "configs/configurations.json")
    frozen_hashes = {
        "runner_script_sha256": ROOT / "scripts/run_rq3_campaign.py",
        "configuration_sha256": ROOT / "configs/configurations.json",
        "protocol_sha256": ROOT / "docs/experimental-protocol.md",
        "topology_plan_sha256": ROOT / "src/mongo_consistency/rq3.py",
        "anchor_manifest_sha256": ROOT / "configs/rq3-anchors.json",
    }
    for field, path in frozen_hashes.items():
        if campaign.get(field) != _sha256(path):
            raise ValueError(f"RQ3 campaign {field} does not match {path.relative_to(ROOT)}")
    if (
        campaign.get("runner_commit") != provenance.get("runner_commit")
        or provenance.get("runner_dirty") is not False
        or campaign.get("protocol_sha256") != provenance.get("protocol_hash")
        or provenance.get("prediction_manifest_hash") != _sha256(ROOT / "configs/predictions.json")
        or campaign.get("anchor_manifest_sha256") != anchor_manifest_digest
    ):
        raise ValueError("RQ3 campaign frozen runtime provenance does not match its manifest")
    preflight_path = input_root.parent / "rq3-preflight.json"
    if not preflight_path.is_file():
        preflight_path = ROOT / "results/raw/rq3-preflight.json"
    if (
        not preflight_path.is_file()
        or campaign.get("preflight_sha256") != _sha256(preflight_path)
    ):
        raise ValueError("RQ3 topology preflight hash does not match the campaign manifest")
    preflight = _read_json(preflight_path)
    if (
        preflight.get("schema_version") != "rq3-preflight.v2"
        or preflight.get("protocol_id") != "rq3-protocol.v2"
        or preflight.get("status") != "PASS"
        or preflight.get("planned_cycle_count") != 10
        or preflight.get("completed_cycle_count") != 10
        or preflight.get("passed_cycle_count") != 10
        or preflight.get("topology_plan") != TOPOLOGY_PLANS["M3"].to_dict()
    ):
        raise ValueError("RQ3 topology rehearsal did not pass its control")
    repetitions = campaign.get("repetitions_per_contrast")
    if type(repetitions) is not int or repetitions != 8:
        raise ValueError("RQ3 campaign repetitions must be exactly eight")
    expected_count = campaign.get("planned_case_count")
    records = campaign.get("records", [])
    if (
        expected_count != repetitions * 6
        or campaign.get("completed_case_count") != expected_count
        or not isinstance(records, list)
        or len(records) != expected_count
    ):
        raise ValueError("RQ3 campaign manifest does not contain every planned history")

    contrast_specs = {
        "M1": ("rq3-m1", {"C5", "C6"}, "RYW", "m1"),
        "M2": ("rq3-m2", {"C8", "C5"}, "WFR", "m2"),
        "M3": ("rq3-m3", {"C3", "C6"}, "MW", "m3"),
    }
    capture_operations = {
        "M1": ["write", "read"],
        "M2": ["read", "write"],
        "M3": ["first_write", "second_write"],
    }
    first_operations = {"M1": "write", "M2": "read", "M3": "first_write"}
    followup_operations = {"M1": "read", "M2": "write", "M3": "second_write"}
    records_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    pair_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
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
            not isinstance(configuration_id, str)
            or configuration_id not in expected_arms
            or record.get("campaign") != campaign_id
            or record.get("property") != property_name
            or record.get("runner_commit") != campaign.get("runner_commit")
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
        path = _history_path(record)
        history_object = read_history(path)
        history = history_object.to_dict()
        if history_object.history_hash != record.get("history_hash"):
            raise ValueError(f"history hash differs from the RQ3 campaign manifest: {path}")
        history_manifest = history.get("manifest", {})
        expected_fields = {
            "trial_id": trial_id,
            "campaign_id": campaign_id,
            "configuration_id": configuration_id,
            "property": property_name,
            "seed": pair_seed,
            "rq3_contrast_id": contrast_id,
            "rq3_pair_id": pair_id,
            "rq3_pair_seed": pair_seed,
            "rq3_replicate": replicate,
            "rq3_protocol_id": "rq3-protocol.v2",
        }
        if any(history_manifest.get(key) != value for key, value in expected_fields.items()):
            raise ValueError(f"history identity differs from its campaign record: {path}")
        if (
            history_manifest.get("runner_commit") != campaign.get("runner_commit")
            or history_manifest.get("runner_dirty") is not False
            or not isinstance(provenance, dict)
            or history_manifest.get("software_versions") != provenance.get("software_versions")
            or history_manifest.get("image_digest") != provenance.get("image_digest")
            or history_manifest.get("checker_version") != provenance.get("checker_version")
            or any(
                history_manifest.get(key) != provenance.get(key)
                for key in (
                    "protocol_commit",
                    "protocol_hash",
                    "prediction_commit",
                    "prediction_manifest_hash",
                )
            )
        ):
            raise ValueError(f"history provenance differs from the RQ3 campaign: {path}")
        if record.get("outcome") == "HARNESS_ERROR" or record.get("runner_error") is not None:
            raise ValueError(f"harness-error history cannot produce RQ3 report evidence: {path}")

        operations = {
            operation.get("operation_id"): operation
            for operation in history.get("operations", [])
            if isinstance(operation, dict) and isinstance(operation.get("operation_id"), str)
        }
        if history_manifest.get("topology_capture_operations") != capture_operations[contrast_id]:
            raise ValueError(f"history topology capture plan differs from {contrast_id}: {path}")
        precondition = history.get("precondition", {})
        precondition_status = precondition.get("status") if isinstance(precondition, dict) else None
        schedule_outcome = history_manifest.get("schedule_outcome")
        for operation_id in capture_operations[contrast_id]:
            operation = operations.get(operation_id)
            if operation is None:
                allowed = precondition_status == "PRECONDITION_MISS"
                if operation_id == followup_operations[contrast_id]:
                    preceding = operations.get(first_operations[contrast_id])
                    allowed = allowed or (
                        preceding is not None
                        and preceding.get("operation_status")
                        in {"UNAVAILABLE", "INDETERMINATE", "HARNESS_ERROR"}
                    )
                    allowed = allowed or (
                        isinstance(schedule_outcome, dict)
                        and schedule_outcome.get("outcome") in {"UNAVAILABLE", "INDETERMINATE"}
                    )
                if not allowed:
                    raise ValueError(f"required RQ3 operation {operation_id} is missing: {path}")
                continue
            if not isinstance(operation.get("topology_before"), dict) or not isinstance(
                operation.get("topology_after"), dict
            ):
                raise TypeError(f"RQ3 operation {operation_id} is missing topology snapshots: {path}")
        history["result"] = record.get("outcome")
        history["source_path"] = path.relative_to(ROOT).as_posix()
        if configuration_id in records_by_pair[pair_id]:
            raise ValueError(f"{pair_id} contains a duplicate configuration arm")
        records_by_pair[pair_id][configuration_id] = history
        pair_records[pair_id].append(record)

    expected_pair_ids = {
        f"{contrast_id.lower()}-r{replicate:02d}"
        for contrast_id in contrast_specs
        for replicate in range(1, repetitions + 1)
    }
    if set(records_by_pair) != expected_pair_ids:
        raise ValueError("RQ3 campaign has missing or unexpected pair IDs")
    for pair_id, items in pair_records.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        expected_arms = contrast_specs[contrast_id][1]
        seeds = [item.get("pair_seed") for item in items]
        arm_configuration_ids = [item.get("configuration_id") for item in items]
        if (
            len(items) != 2
            or set(arm_configuration_ids) != expected_arms
            or any(type(seed) is not int for seed in seeds)
            or seeds[0] != seeds[1]
        ):
            raise ValueError(f"{pair_id} does not contain the registered matched-seed arms")

    recomputed_controls: dict[tuple[str, str], dict[str, Any]] = {}
    for pair_id, arms in records_by_pair.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        recomputed_controls[(contrast_id, pair_id)] = pair_control(
            contrast_id,
            pair_id,
            arms,
            configurations=configuration_settings,
        )
    recorded_controls = campaign.get("pair_controls")
    if not isinstance(recorded_controls, list):
        raise ValueError("RQ3 v2 campaign is missing its pair-control records")
    recorded_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in recorded_controls:
        if not isinstance(item, dict):
            raise TypeError("RQ3 campaign contains a non-object pair-control record")
        key = (str(item.get("contrast_id")), str(item.get("pair_id")))
        if key in recorded_by_key:
            raise ValueError(f"RQ3 campaign contains a duplicate pair-control record: {key}")
        recorded_by_key[key] = item
    if set(recorded_by_key) != set(recomputed_controls):
        raise ValueError("RQ3 campaign pair-control records do not cover the planned pairs")
    for key, recomputed in recomputed_controls.items():
        if recorded_by_key[key] != recomputed:
            raise ValueError(
                f"RQ3 campaign pair-control record differs from raw histories for {key[1]}"
            )

    grouped: dict[str, list[dict[str, Any]]] = {"M1": [], "M2": [], "M3": []}
    for pair_id, arms in records_by_pair.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        expected_arms = contrast_specs[contrast_id][1]
        if set(arms) != expected_arms:
            raise ValueError(f"{pair_id} does not contain the registered configuration pair")
        grouped[contrast_id].append(
            _row_for_pair(contrast_id, pair_id, arms, configuration_settings)
        )

    selections: dict[str, Any] = {}
    summary_contrasts: dict[str, Any] = {}
    for contrast_id, pairs in grouped.items():
        pairs.sort(key=lambda pair: pair["pair_id"])
        if len(pairs) != campaign.get("repetitions_per_contrast"):
            raise ValueError(f"{contrast_id} is missing matched pairs")
        valid_pairs = _valid_pairs(pairs)
        chosen = valid_pairs[0] if valid_pairs else None
        selection = None
        if chosen is not None:
            configuration_order = {
                "M1": ("C5", "C6"),
                "M2": ("C8", "C5"),
                "M3": ("C3", "C6"),
            }[contrast_id]
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
                    for configuration_id in configuration_order
                ],
            }
        selections[contrast_id] = selection
        invalid_pairs = [
            {
                "pair_id": pair["pair_id"],
                "invalid_reasons": pair["invalid_reasons"],
            }
            for pair in pairs
            if not pair["control_valid"]
        ]
        contrast_summary = {
            "planned_pair_count": len(pairs),
            "control_valid_pair_count": len(valid_pairs),
            "invalid_pair_count": len(invalid_pairs),
            "invalid_pairs": invalid_pairs,
            "signature_counts": _counts(valid_pairs, contrast_id),
            "selected_pair": selection,
        }
        summary_contrasts[contrast_id] = contrast_summary

    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    output_root.mkdir(parents=True, exist_ok=True)
    selection_manifest = {
        "schema_version": "rq3-selection.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
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
    (output_root / "selection-manifest.json").write_text(
        json.dumps(selection_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    generated_section = _render_report_section(grouped, selections, campaign, historical_anchors)
    section_path = submission_root / "generated-rq3.tex"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(generated_section, encoding="utf-8")
    timeline_path = submission_root / "figures/rq3-causal-timeline.pdf"
    if selections["M1"] is not None:
        causal_pair = next(
            pair for pair in _valid_pairs(grouped["M1"])
            if pair["pair_id"] == selections["M1"]["pair_id"]
        )
        _render_timeline(causal_pair, timeline_path)
    generated_artifacts = {
        "report_tex": {
            "path": section_path.relative_to(ROOT).as_posix(),
            "sha256": _sha256(section_path),
        },
    }
    if selections["M1"] is not None:
        generated_artifacts["timeline_pdf"] = {
            "path": timeline_path.relative_to(ROOT).as_posix(),
            "sha256": _sha256(timeline_path),
        }
    summary = {
        "schema_version": "rq3-analysis.v2",
        "protocol_id": "rq3-protocol.v2",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "campaign_manifest_sha256": manifest_digest,
        "preflight_sha256": campaign["preflight_sha256"],
        "anchor_manifest_sha256": anchor_manifest_digest,
        "historical_anchors": historical_anchors,
        "repetitions_per_contrast": campaign["repetitions_per_contrast"],
        "contrast_summaries": summary_contrasts,
        "selection_manifest": (output_root / "selection-manifest.json")
        .relative_to(ROOT)
        .as_posix(),
        "generated_artifacts": generated_artifacts,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _render_report_section(
    grouped: dict[str, list[dict[str, Any]]],
    selections: dict[str, Any],
    campaign: dict[str, Any],
    historical_anchors: list[dict[str, Any]] | None = None,
) -> str:
    if historical_anchors is None:
        anchor_manifest, _ = verify_anchor_manifest(ROOT)
        historical_anchors = _summarize_historical_anchors(anchor_manifest)
    specifications = {
        "M1": ("causal session", "C5 vs C6; off vs on"),
        "M2": ("read concern", "C8 vs C5; local vs majority"),
        "M3": ("write concern", "C3 vs C6; w:1 vs majority"),
    }
    rows: list[str] = []
    validity: list[str] = []
    for contrast_id in ("M1", "M2", "M3"):
        planned_pairs = grouped[contrast_id]
        valid_pairs = _valid_pairs(planned_pairs)
        planned_n = len(planned_pairs)
        valid_n = len(valid_pairs)
        counts = _counts(valid_pairs, contrast_id)
        mechanism, contrast = specifications[contrast_id]
        validity.append(
            f"{contrast_id}: Control-valid pairs {valid_n}/{planned_n}"
        )

        if contrast_id == "M1":
            observation = (
                f"C5 returned stale v0 without afterClusterTime in "
                f"{counts['C5_stale_success_without_after_cluster_time']}/{valid_n}; "
                f"C6 carried afterClusterTime in "
                f"{counts['C6_after_cluster_time_present']}/{valid_n}, with no "
                f"successful stale read and unavailable reads in "
                f"{counts['C6_unavailable_reads']}/{valid_n}."
            )
            interpretation = (
                "Consistent with a causal-session lower bound preventing a stale "
                "successful read; the client-visible timeout does not prove internal "
                "server blocking."
            )
        elif contrast_id == "M2":
            observation = (
                f"C8 local returned v1 in {counts['C8_local_read_v1']}/{valid_n}; "
                f"C5 majority returned v0 in {counts['C5_majority_read_v0']}/{valid_n}. "
                f"The returned version was present in final state for C8/C5 in "
                f"{counts['C8_read_version_present_in_final_state']}/{valid_n} and "
                f"{counts['C5_read_version_present_in_final_state']}/{valid_n}."
            )
            interpretation = (
                "Consistent with local exposing the latest state at the selected "
                "instance and majority exposing a majority-committed state. The "
                "setup W1 was a protocol-defined w:1 stimulus, not independently "
                "observed command evidence; the trace supports a role/state "
                "observation rather than a universal route effect."
            )
        else:
            observation = (
                f"C3 acknowledged W1 at w:1 in "
                f"{counts['C3_w1_first_write_acknowledged']}/{valid_n}; the "
                f"acknowledged W1 was absent from all converged members in "
                f"{counts['C3_acknowledged_w1_absent_from_all_converged_members']}/{valid_n}. "
                f"C6 majority W1 timed out in "
                f"{counts['C6_majority_first_write_network_timeout']}/{valid_n}; "
                f"direct W1 presence after healing was observed in "
                f"{counts['C6_w1_present_on_all_final_members_after_timeout']}/{valid_n}."
            )
            interpretation = (
                "w:1 acknowledgement and majority acknowledgement expose different "
                "client outcomes under failover. A timeout leaves the write effect "
                "unresolved from the client response; it does not establish definite "
                "failure or internal waiting."
            )

        rows.append(
            " & ".join(
                _latex_escape(cell)
                for cell in (mechanism, contrast, observation, interpretation)
            )
            + r" \\"
        )

    m1_valid = _valid_pairs(grouped["M1"])
    timeline_note = ""
    if m1_valid:
        timeline_note = (
            "\\maybefigure[fig:rq3-causal-timeline]"
            "{submission/figures/rq3-causal-timeline.pdf}"
            "{C5/C6 causal-session timeline; each arm is an independent fault episode.}"
        )
    repetitions = int(campaign["repetitions_per_contrast"])
    validity_text = "; ".join(validity) + "."
    return rf"""\subsection{{Mechanism contrasts (RQ3)}}

RQ3 is a focused mechanism study that explains representative RQ1 and RQ2
observations; it is not a third benchmark. The protocol planned {repetitions}
matched-seed pairs per contrast, and the analyzer recomputes pair validity from
the raw histories before deriving the observations below. {validity_text} The
six registered RQ1 histories remain historical anchors rather than replay
denominators. The four client-centric definitions used by the checker---read
your-writes, monotonic reads, monotonic writes, and writes-follow-reads---are
client properties from Lecture~3; MongoDB read concern, write concern, and
causal sessions are the mechanisms used to interpret them.

{{\scriptsize
\setlength{{\tabcolsep}}{{3pt}}
\begin{{longtable}}{{@{{}}p{{2.2cm}}p{{2.8cm}}p{{5.8cm}}p{{5.2cm}}@{{}}}}
\caption{{RQ3 focused mechanism contrasts; observations use control-valid pairs.}}
\label{{tab:rq3-mechanisms}}\\
\toprule
Mechanism & Contrast & Observation & Interpretation / limitation \\
\midrule
\endfirsthead
\caption[]{{RQ3 focused mechanism contrasts (continued).}}\\
\toprule
Mechanism & Contrast & Observation & Interpretation / limitation \\
\midrule
\endhead
\bottomrule
\endfoot
{chr(10).join(rows)}
\end{{longtable}}
}}

The contrasts are finite, Docker-based observations. M2 is interpreted at the
level of role-equivalent replica states and does not establish a general
physical-member or route effect. M3's client-visible timeout does not reveal
whether MongoDB waited internally, and direct presence after healing is a
separate observation from acknowledgement. The repetitions do not prove a
universal guarantee, and containers on one host do not reproduce a WAN failure
model. Raw hashes, routes, pair controls, and selected histories remain in the
analysis and reproduction artifacts.

{timeline_note}
"""

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--submission-root", type=Path, default=ROOT / "submission")
    args = parser.parse_args()
    summary = _summary_and_report(
        input_root=args.input_root,
        output_root=args.output_root,
        submission_root=args.submission_root,
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
