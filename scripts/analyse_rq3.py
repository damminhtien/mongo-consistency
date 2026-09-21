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

from mongo_consistency.history import read_history

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


def _faulted_members(history: dict[str, Any]) -> tuple[str, ...]:
    for event in history.get("fault_events", []):
        if not isinstance(event, dict):
            continue
        if event.get("action") == "isolate":
            members = event.get("members", [])
            if event.get("status") != "APPLIED" or event.get("rules_verified") is not True:
                return ()
            if not isinstance(members, list) or not members:
                return ()
            return tuple(sorted(str(member) for member in members))
    return ()


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


def _matched_topology(pair: dict[str, dict[str, Any]]) -> bool:
    left, right = pair.values()
    left_primary = _initial_topology(left).get("primary")
    right_primary = _initial_topology(right).get("primary")
    left_faulted = _faulted_members(left)
    right_faulted = _faulted_members(right)
    return (
        isinstance(left_primary, str)
        and left_primary in {"mongo1", "mongo2", "mongo3"}
        and left_primary == right_primary
        and bool(left_faulted)
        and left_faulted == right_faulted
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


def _role_state_signature(history: dict[str, Any], operation_id: str) -> tuple[Any, ...] | None:
    operation = _operation(history, operation_id)
    topology = operation.get("topology_before")
    members = topology.get("members") if isinstance(topology, dict) else None
    primary = topology.get("primary") if isinstance(topology, dict) else None
    faulted = set(_faulted_members(history))
    address = operation.get("actual_server_address")
    route = _member_from_address(address) if isinstance(address, str) else None
    member_names = {"mongo1", "mongo2", "mongo3"}
    if (
        not isinstance(topology, dict)
        or topology.get("stable") is not True
        or not isinstance(members, dict)
        or set(members) != member_names
        or not isinstance(primary, str)
        or primary not in members
        or len(faulted) != 1
        or not faulted.issubset(members)
        or operation.get("operation_status") not in {"SUCCESS", "UNAVAILABLE", "INDETERMINATE"}
        or not isinstance(address, str)
        or route not in members
    ):
        return None
    commit = topology.get("last_committed_op_time")
    if _timestamp_relation(commit, commit) != "at":
        return None
    if any(not isinstance(member, dict) for member in members.values()):
        return None
    roles = [member.get("role") for member in members.values()]
    if roles.count("PRIMARY") != 1 or roles.count("SECONDARY") != 2:
        return None
    state_relations = {
        member_name: _timestamp_relation(member.get("last_write_op_time"), commit)
        for member_name, member in members.items()
    }
    if any(relation not in {"ahead", "behind", "at"} for relation in state_relations.values()):
        return None
    state_by_role = tuple(
        sorted(
            (
                str(member["role"]),
                member_name in faulted,
                state_relations[member_name],
            )
            for member_name, member in members.items()
        )
    )
    route_member = members.get(route) if isinstance(route, str) else None
    return (
        len(members),
        state_by_role,
        primary in faulted,
        route_member.get("role") if isinstance(route_member, dict) else None,
        route in faulted if isinstance(route, str) else None,
        route == primary if isinstance(route, str) else None,
    )


def _role_state_audit(pair: dict[str, Any], contrast_id: str) -> dict[str, bool]:
    operation_id = {"M1": "read", "M2": "read", "M3": "first_write"}[contrast_id]
    left_id, right_id = {
        "M1": ("C5", "C6"),
        "M2": ("C8", "C5"),
        "M3": ("C3", "C6"),
    }[contrast_id]
    arms = pair["arms"]
    left = _role_state_signature(arms[left_id], operation_id)
    right = _role_state_signature(arms[right_id], operation_id)
    eligible = left is not None and right is not None
    return {"eligible": eligible, "matched": eligible and left == right}


def _m2_timestamp_state_pattern(
    history: dict[str, Any], expected_concern: str, expected_version: int
) -> bool:
    operation = _operation(history, "read")
    topology = operation.get("topology_before")
    members = topology.get("members") if isinstance(topology, dict) else None
    primary = topology.get("primary") if isinstance(topology, dict) else None
    faulted = set(_faulted_members(history))
    address = operation.get("actual_server_address")
    route = _member_from_address(address) if isinstance(address, str) else None
    commit = topology.get("last_committed_op_time") if isinstance(topology, dict) else None
    if (
        operation.get("operation_status") != "SUCCESS"
        or operation.get("read_concern") != expected_concern
        or operation.get("observed_version") != expected_version
        or not isinstance(topology, dict)
        or topology.get("stable") is not True
        or not isinstance(members, dict)
        or not isinstance(primary, str)
        or primary not in members
        or len(faulted) != 1
        or primary not in faulted
        or not faulted.issubset(members)
        or route != primary
        or not isinstance(address, str)
        or set(members) != {"mongo1", "mongo2", "mongo3"}
        or any(not isinstance(member, dict) for member in members.values())
    ):
        return False
    primary_state = members[primary]
    secondaries = [member for name, member in members.items() if name != primary]
    return (
        primary_state.get("role") == "PRIMARY"
        and _timestamp_relation(primary_state.get("last_write_op_time"), commit) == "ahead"
        and all(
            member.get("role") == "SECONDARY"
            and _timestamp_relation(member.get("last_write_op_time"), commit) == "at"
            for member in secondaries
        )
    )


def _m2_timestamp_state_pattern_matched(pair: dict[str, Any]) -> bool:
    arms = pair["arms"]
    return _m2_timestamp_state_pattern(arms["C8"], "local", 1) and _m2_timestamp_state_pattern(
        arms["C5"], "majority", 0
    )


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


def _row_for_pair(contrast_id: str, pair_id: str, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    left_id, right_id = {
        "M1": ("C5", "C6"),
        "M2": ("C8", "C5"),
        "M3": ("C3", "C6"),
    }[contrast_id]
    role_state_audit = _role_state_audit({"arms": arms}, contrast_id)
    row = {
        "pair_id": pair_id,
        "arms": arms,
        "left_configuration": left_id,
        "right_configuration": right_id,
        "topology_matched": _matched_topology(arms),
        "role_state_eligible": role_state_audit["eligible"],
        "role_state_matched": role_state_audit["matched"],
    }
    if contrast_id == "M2":
        row["m2_timestamp_state_pattern_matched"] = _m2_timestamp_state_pattern_matched(
            {"arms": arms}
        )
    return row


def _score_pair(contrast_id: str, pair: dict[str, Any]) -> int:
    arms = pair["arms"]
    left = arms[pair["left_configuration"]]
    right = arms[pair["right_configuration"]]
    score = 2 if pair["topology_matched"] else 0
    if contrast_id == "M1":
        left_read = _read_signature(left)
        right_read = _read_signature(right)
        score += int(left_read["status"] == "SUCCESS" and left_read["version"] == 0)
        score += int(left_read["after_cluster_time"] is None)
        score += int(right_read["after_cluster_time"] is not None)
        score += int(
            right_read["status"] in {"UNAVAILABLE", "INDETERMINATE"}
            or (
                right_read["status"] == "SUCCESS"
                and isinstance(right_read["version"], int)
                and right_read["version"] >= 1
            )
        )
        score += int(
            left_read["route"] is not None and left_read["route"] == right_read["route"]
        )
    elif contrast_id == "M2":
        local_read = _read_signature(left)
        majority_read = _read_signature(right)
        score += int(local_read["status"] == "SUCCESS" and local_read["version"] == 1)
        score += int(majority_read["status"] == "SUCCESS" and majority_read["version"] == 0)
        score += int(
            local_read["route"] is not None and local_read["route"] == majority_read["route"]
        )
    else:
        weak_write = _write_signature(left, "first_write")
        majority_write = _write_signature(right, "first_write")
        score += int(weak_write["status"] == "SUCCESS")
        score += int(majority_write["status"] in {"UNAVAILABLE", "INDETERMINATE"})
        score += int(_final_write_presence(left, "w1") == "no_members")
        score += int(_final_write_presence(right, "w1") == "all_members")
    return score


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
    if campaign.get("status") != "COMPLETE":
        raise ValueError(f"RQ3 campaign is not complete: {campaign.get('status')}")
    provenance = campaign.get("runtime_provenance")
    if not isinstance(provenance, dict):
        raise TypeError("RQ3 campaign runtime provenance is missing")
    frozen_hashes = {
        "runner_script_sha256": ROOT / "scripts/run_rq3_campaign.py",
        "configuration_sha256": ROOT / "configs/configurations.json",
        "protocol_sha256": ROOT / "docs/experimental-protocol.md",
    }
    for field, path in frozen_hashes.items():
        if campaign.get(field) != _sha256(path):
            raise ValueError(f"RQ3 campaign {field} does not match {path.relative_to(ROOT)}")
    if (
        campaign.get("runner_commit") != provenance.get("runner_commit")
        or provenance.get("runner_dirty") is not False
        or campaign.get("protocol_sha256") != provenance.get("protocol_hash")
        or provenance.get("prediction_manifest_hash") != _sha256(ROOT / "configs/predictions.json")
    ):
        raise ValueError("RQ3 campaign frozen runtime provenance does not match its manifest")
    repetitions = campaign.get("repetitions_per_contrast")
    if type(repetitions) is not int or not 5 <= repetitions <= 10:
        raise ValueError("RQ3 campaign repetitions must be between five and ten")
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
        configurations = [item.get("configuration_id") for item in items]
        if (
            len(items) != 2
            or set(configurations) != expected_arms
            or any(type(seed) is not int for seed in seeds)
            or seeds[0] != seeds[1]
        ):
            raise ValueError(f"{pair_id} does not contain the registered matched-seed arms")

    grouped: dict[str, list[dict[str, Any]]] = {"M1": [], "M2": [], "M3": []}
    for pair_id, arms in records_by_pair.items():
        contrast_id = pair_id.split("-", maxsplit=1)[0].upper()
        expected_arms = contrast_specs[contrast_id][1]
        if set(arms) != expected_arms:
            raise ValueError(f"{pair_id} does not contain the registered configuration pair")
        grouped[contrast_id].append(_row_for_pair(contrast_id, pair_id, arms))

    selections: dict[str, Any] = {}
    summary_contrasts: dict[str, Any] = {}
    for contrast_id, pairs in grouped.items():
        pairs.sort(key=lambda pair: pair["pair_id"])
        if len(pairs) != campaign.get("repetitions_per_contrast"):
            raise ValueError(f"{contrast_id} is missing matched pairs")
        chosen = min(
            pairs,
            key=lambda pair: (-_score_pair(contrast_id, pair), pair["pair_id"]),
        )
        selections[contrast_id] = {
            "pair_id": chosen["pair_id"],
            "topology_matched": chosen["topology_matched"],
            "role_state_eligible": chosen["role_state_eligible"],
            "role_state_matched": chosen["role_state_matched"],
            "score": _score_pair(contrast_id, chosen),
            "histories": [
                {
                    "trial_id": _history_id(history),
                    "path": history["source_path"],
                    "sha256": history["history_hash"],
                    "configuration_id": history["manifest"]["configuration_id"],
                }
                for history in chosen["arms"].values()
            ],
        }
        contrast_summary = {
            "pair_count": len(pairs),
            "topology_matched_pairs": sum(pair["topology_matched"] for pair in pairs),
            "role_state_auditable_pairs": sum(pair["role_state_eligible"] for pair in pairs),
            "role_state_unobserved_pairs": sum(not pair["role_state_eligible"] for pair in pairs),
            "role_state_matched_pairs": sum(pair["role_state_matched"] for pair in pairs),
            "same_named_topology_signature_counts": _counts(
                [pair for pair in pairs if pair["topology_matched"]], contrast_id
            ),
            "signature_counts": _counts(pairs, contrast_id),
            "selected_pair": selections[contrast_id],
        }
        if contrast_id == "M2":
            contrast_summary["m2_timestamp_state_pattern_matched_pairs"] = sum(
                pair["m2_timestamp_state_pattern_matched"] for pair in pairs
            )
        summary_contrasts[contrast_id] = contrast_summary

    causal_pair = next(
        pair
        for pair in grouped["M1"]
        if pair["pair_id"] == selections["M1"]["pair_id"]
    )
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    output_root.mkdir(parents=True, exist_ok=True)
    selection_manifest = {
        "schema_version": "rq3-selection.v1",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "campaign_manifest_sha256": manifest_digest,
        "selected_pairs": selections,
    }
    (output_root / "selection-manifest.json").write_text(
        json.dumps(selection_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    generated_section = _render_report_section(grouped, selections, campaign)
    section_path = submission_root / "generated-rq3.tex"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(generated_section, encoding="utf-8")
    _render_timeline(
        causal_pair,
        submission_root / "figures/rq3-causal-timeline.pdf",
    )
    generated_artifacts = {
        "report_tex": {
            "path": section_path.relative_to(ROOT).as_posix(),
            "sha256": _sha256(section_path),
        },
        "timeline_pdf": {
            "path": (submission_root / "figures/rq3-causal-timeline.pdf")
            .relative_to(ROOT)
            .as_posix(),
            "sha256": _sha256(submission_root / "figures/rq3-causal-timeline.pdf"),
        },
    }
    summary = {
        "schema_version": "rq3-analysis.v1",
        "campaign_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "campaign_manifest_sha256": manifest_digest,
        "role_state_audit_basis": (
            "Post-hoc member-name permutation over role, fault, route, and recorded "
            "lastWrite/commit timestamp components; per-OpTime terms are not retained."
        ),
        "repetitions_per_contrast": campaign["repetitions_per_contrast"],
        "contrast_summaries": summary_contrasts,
        "selection_manifest": "results/analysis/rq3/selection-manifest.json",
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
) -> str:
    repetitions = int(campaign["repetitions_per_contrast"])
    specifications = {
        "M1": ("C5/C6", "causal: off/on"),
        "M2": ("C8/C5", "read concern: local/majority"),
        "M3": ("C3/C6", "write concern: w:1/majority"),
    }
    rows = []
    for contrast_id in ("M1", "M2", "M3"):
        pairs = grouped[contrast_id]
        counts = _counts(pairs, contrast_id)
        left, changed = specifications[contrast_id]
        n = repetitions
        if contrast_id == "M1":
            successful_reads = counts["C6_successful_read_responses"]
            error_codes = ", ".join(
                f"{code}: {count}"
                for code, count in counts["C6_read_error_code_counts"].items()
            ) or "none recorded"
            successful_value_summary = (
                f"successful without a version: {counts['C6_successful_reads_without_version']}; "
                f"nonstale concrete values: {counts['C6_nonstale_successful_reads']}/"
                f"{successful_reads}; stale among successful: "
                f"{counts['C6_stale_successful_reads']}/{successful_reads}."
                if successful_reads
                else "no successful C6 read values to classify."
            )
            evidence = (
                f"C5 stale v0/no afterClusterTime: "
                f"{counts['C5_stale_success_without_after_cluster_time']}/{n}; "
                f"W1 operationTime timestamp component greater than routed-member "
                f"lastWrite timestamp component: "
                f"{counts['C5_write_time_ahead_of_routed_member_last_write']}/{n}; "
                f"C6 afterClusterTime present: {counts['C6_after_cluster_time_present']}/{n}, "
                f"equal to W1 operationTime timestamp component: "
                f"{counts['C6_after_cluster_time_matches_write_time']}/{n}, "
                f"greater than routed-member lastWrite timestamp component: "
                f"{counts['C6_after_cluster_time_ahead_of_routed_member_last_write']}/{n}; "
                f"C6 reads issued: {counts['C6_read_attempted']}/{n}, "
                f"successful: {successful_reads}/{n}, "
                f"unavailable: {counts['C6_unavailable_reads']}/{n}, "
                f"indeterminate: {counts['C6_indeterminate_reads']}/{n}, "
                f"not reached: {counts['C6_read_not_reached']}/{n}; "
                f"error codes: {error_codes}; {successful_value_summary}"
            )
        elif contrast_id == "M2":
            evidence = (
                f"C8 local v1: {counts['C8_local_read_v1']}/{n} "
                f"(successful reads: {counts['C8_successful_read_responses']}/{n}); "
                f"C5 majority v0: {counts['C5_majority_read_v0']}/{n} "
                f"(successful reads: {counts['C5_successful_read_responses']}/{n}); "
                f"recorded timestamp-component pattern: "
                f"{sum(pair['m2_timestamp_state_pattern_matched'] for pair in pairs)}/{n}; "
                f"W2 acknowledged: C8 {counts['C8_W2_acknowledged']}/{n}, "
                f"C5 {counts['C5_W2_acknowledged']}/{n}; non-success: "
                f"C8 {counts['C8_W2_non_success']}/{n}, "
                f"C5 {counts['C5_W2_non_success']}/{n}; W2 on all final members: "
                f"C8 {counts['C8_W2_present_on_all_final_members']}/{n}, "
                f"C5 {counts['C5_W2_present_on_all_final_members']}/{n}; "
                f"read version retained after convergence: "
                f"C8 {counts['C8_read_version_present_in_final_state']}/{n}, "
                f"C5 {counts['C5_read_version_present_in_final_state']}/{n}."
            )
        else:
            evidence = (
                f"C3 w:1 first-write acknowledgements: {counts['C3_w1_first_write_acknowledged']}/{n}; "
                f"acknowledged W1 absent from every converged post-heal member: "
                f"{counts['C3_acknowledged_w1_absent_from_all_converged_members']}/{n}; "
                f"C3 converged final states: {counts['C3_converged_final_observations']}/{n}; "
                f"C6 majority acknowledgements: {counts['C6_majority_first_write_acknowledged']}/{n}; "
                f"C6 majority timeouts: {counts['C6_majority_first_write_network_timeout']}/{n}; "
                f"W1 later present on all converged members after NetworkTimeout: "
                f"{counts['C6_w1_present_on_all_final_members_after_timeout']}/{n}; "
                f"C6 converged final states: {counts['C6_converged_final_observations']}/{n}."
            )
        topology_matched = sum(pair["topology_matched"] for pair in pairs)
        role_state_auditable = sum(pair["role_state_eligible"] for pair in pairs)
        role_state_matched = sum(pair["role_state_matched"] for pair in pairs)
        role_state_summary = (
            f"{role_state_matched}/{role_state_auditable} auditable pairs"
            if role_state_auditable
            else "no auditable pairs"
        )
        route_operation = "first-write" if contrast_id == "M3" else "read"
        route_match_count = counts[
            "same_actual_first_write_route_pairs"
            if contrast_id == "M3"
            else "same_actual_read_route_pairs"
        ]
        evidence += (
            f" Same-named initial primary/isolation target: {topology_matched}/{n}; "
            f"same actual {route_operation} route: {route_match_count}/{n}; "
            f"role/timestamp signature isomorphic under member-name permutation (post-hoc): "
            f"{role_state_summary}; "
            f"unobserved: {n - role_state_auditable}/{n}."
        )
        chosen = selections[contrast_id]
        matched = "same-named member pair" if chosen["topology_matched"] else "member names differ"
        rows.append(
            f"{contrast_id} ({_latex_escape(left)}) & {_latex_escape(changed)} & "
            f"{_latex_escape(evidence)} & {_latex_escape(matched)}; "
            f"selected \\texttt{{{_latex_escape(chosen['pair_id'])}}} \\\\"
        )
    m2_pairs = grouped["M2"]
    m2_counts = _counts(m2_pairs, "M2")
    m2_topology_matches = sum(pair["topology_matched"] for pair in m2_pairs)
    m2_role_state_matches = sum(pair["role_state_matched"] for pair in m2_pairs)
    m2_role_state_auditable = sum(pair["role_state_eligible"] for pair in m2_pairs)
    m2_role_state_summary = (
        f"{m2_role_state_matches}/{m2_role_state_auditable} auditable pairs"
        if m2_role_state_auditable
        else "no auditable pairs"
    )
    m2_note = (
        f"M2 classifications: C8 {m2_counts['C8_WFR_violations']} WFR violations, "
        f"{m2_counts['C8_WFR_indeterminate']} indeterminate; C5 "
        f"{m2_counts['C5_WFR_passes']} WFR passes, "
        f"{m2_counts['C5_WFR_indeterminate']} indeterminate. W2 was present on "
        f"all final members in C8 {m2_counts['C8_W2_present_on_all_final_members']}/"
        f"{repetitions} and C5 {m2_counts['C5_W2_present_on_all_final_members']}/"
        f"{repetitions} pairs. C8's returned v1 is absent from the converged final "
        f"versions in {repetitions - m2_counts['C8_read_version_present_in_final_state']}/"
        f"{repetitions} pairs; C5's returned v0 remains in "
        f"{m2_counts['C5_read_version_present_in_final_state']}/{repetitions}. "
        f"Same-named initial primary/isolation targets: {m2_topology_matches}/"
        f"{repetitions}; post-hoc role/timestamp-state audit: "
        f"{m2_role_state_summary}; "
        f"{repetitions - m2_role_state_auditable}/{repetitions} unobserved. The "
        f"combined C8-local-v1/C5-majority-v0 outcome and recorded timestamp-component "
        f"pattern (both reads routed to their isolated primary; the primary lastWrite "
        f"timestamp component exceeded the commit timestamp component, while both "
        f"secondary components equaled it) occurred in "
        f"{sum(pair['m2_timestamp_state_pattern_matched'] for pair in m2_pairs)}"
        f"/{repetitions} pairs. Interpret this under the explicit member-name "
        "permutation assumption, not as same-host matching."
    )
    m3_exact_pairs = [pair for pair in grouped["M3"] if pair["topology_matched"]]
    m3_exact_counts = _counts(m3_exact_pairs, "M3")
    m3_role_state_matches = sum(pair["role_state_matched"] for pair in grouped["M3"])
    m3_role_state_auditable = sum(pair["role_state_eligible"] for pair in grouped["M3"])
    m3_role_state_summary = (
        f"{m3_role_state_matches}/{m3_role_state_auditable} auditable pairs"
        if m3_role_state_auditable
        else "no auditable pairs"
    )
    if m3_exact_pairs:
        m3_note = (
            f"In the {len(m3_exact_pairs)} same-named M3 pairs, C3 acknowledged W1 "
            f"and W1 was absent from every converged member in "
            f"{m3_exact_counts['C3_acknowledged_w1_absent_from_all_converged_members']}/"
            f"{len(m3_exact_pairs)}; C6 timed out on its majority W1 in "
            f"{m3_exact_counts['C6_majority_first_write_network_timeout']}/"
            f"{len(m3_exact_pairs)} and W1 was present on all final members in "
            f"{m3_exact_counts['C6_w1_present_on_all_final_members_after_timeout']}/"
            f"{len(m3_exact_pairs)}. The post-hoc role/timestamp-state audit matched "
            f"{m3_role_state_summary} under member-name permutation."
        )
    else:
        m3_note = (
            "No same-named M3 pairs were available for the exact-topology outcome "
            "comparison. The post-hoc role/timestamp-state audit matched "
            f"{m3_role_state_summary} under member-name permutation."
        )
    m1_counts = _counts(grouped["M1"], "M1")
    m1_errors = ", ".join(
        f"{code}: {count}"
        for code, count in m1_counts["C6_read_error_code_counts"].items()
    ) or "none recorded"
    m1_note = (
        f"C5 returned stale v0 without afterClusterTime in "
        f"{m1_counts['C5_stale_success_without_after_cluster_time']}/{repetitions} "
        f"histories; W1's operationTime timestamp component was greater than the "
        f"routed member's directly observed lastWrite timestamp component in "
        f"{m1_counts['C5_write_time_ahead_of_routed_member_last_write']}/"
        f"{repetitions}. In C6, afterClusterTime matched W1's operationTime "
        f"timestamp component in {m1_counts['C6_after_cluster_time_matches_write_time']}/"
        f"{repetitions} and was greater than the routed member's recorded lastWrite "
        f"timestamp component at the pre-read snapshot in "
        f"{m1_counts['C6_after_cluster_time_ahead_of_routed_member_last_write']}/"
        f"{repetitions}; reads ended UNAVAILABLE with no value in "
        f"{m1_counts['C6_unavailable_reads']}/{repetitions} histories (error codes: "
        f"{m1_errors}). This is consistent with a causal lower bound constraining "
        "read completion, but the snapshots and timeout do not prove an internal "
        "server-side wait. Causal ordering constrains when a read may complete; "
        "it does not make replication instantaneous."
    )
    return rf"""\subsection{{Mechanism contrasts (RQ3)}}

The study replays {repetitions} matched-seed pairs per contrast. Each arm is a
separate history and fault episode, so the seed and schedule are matched while
the physical injections are independent. The counts below include every
planned pair; topology matches are reported explicitly. The term and role
snapshots are direct observations \cite{{mongodb-replset-status}}. The
interpretations are consistent with documented causal-session, read-concern,
and write-concern behavior
\cite{{mongodb-causal-consistency,mongodb-read-concern,mongodb-write-concern}};
they do not expose MongoDB's internal wait or replication state. Same-named
member matching was preregistered; the supplementary role/state isomorphism
audit is post-hoc and treats symmetric replica names as interchangeable labels.
The three contrasts were motivated by six retrospective RQ1 anchor histories;
their routes, outcomes, hashes, and selection limits are documented in
\texttt{{docs/rq3-historical-trace-selection.md}}. Those anchors are not included
in the replay denominators.
The retained member lastWrite and majority-commit values include timestamp
components but omit each OpTime's term, so timestamp-component comparisons below
do not establish full term-aware OpTime order.

{{\scriptsize
\setlength{{\tabcolsep}}{{3pt}}
\begin{{longtable}}{{@{{}}p{{1.7cm}}p{{2.5cm}}p{{7.8cm}}p{{2.4cm}}@{{}}}}
\caption{{RQ3 matched-seed mechanism contrasts. Counts use all {repetitions} pairs per contrast.}}
\label{{tab:rq3-mechanisms}}\\
\toprule
Pair & Changed factor & Observed signatures & Topology / exemplar \\
\midrule
\endfirsthead
\caption[]{{RQ3 matched-seed mechanism contrasts (continued).}}\\
\toprule
Pair & Changed factor & Observed signatures & Topology / exemplar \\
\midrule
\endhead
\bottomrule
\endfoot
{'\n'.join(rows)}
\end{{longtable}}
}}

The C3/C6 write comparison distinguishes acknowledgement from effect: a
\texttt{{w:1}} acknowledgement followed by W1 missing from every converged,
post-heal member is an observed rollback; a majority-write timeout remains
unresolved until the direct final-state observation. The C5/C6 causal comparison
records the command's causal time bound, direct routed-member state, and client
response separately. Read concern is interpreted from the value and route
actually recorded for each WFR read.
The frozen protocol and runner specify \texttt{{w:1}} for M2's setup W1. The raw
setup diagnostic does not retain its command event, so the outgoing concern is
not independently observed in these histories.

{_latex_escape(m1_note)}

{_latex_escape(m2_note)}

{_latex_escape(m3_note)}

\maybefigure[fig:rq3-causal-timeline]{{submission/figures/rq3-causal-timeline.pdf}}{{Matched-seed C5/C6 RYW trace. The two arms use independent fault episodes; client command metadata, direct topology snapshots, and response outcomes are shown from the selected histories.}}
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
