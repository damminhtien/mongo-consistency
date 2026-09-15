"""Load the committed experiment configuration without third-party parsers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
CONFIGURATION_IDS = tuple(f"C{number}" for number in range(1, 9))
PROPERTIES = ("RYW", "MR", "MW", "WFR")


def load_json(path: Path) -> dict[str, Any]:
    """Load one UTF-8 JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def load_configurations(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and validate the complete C1-C8 matrix."""

    payload = load_json(path or CONFIG_ROOT / "configurations.json")
    configurations = payload.get("configurations")
    if not isinstance(configurations, list):
        raise ValueError("configurations must be a list")
    result = {str(item["id"]): dict(item) for item in configurations}
    if tuple(result) != CONFIGURATION_IDS:
        raise ValueError(f"configuration IDs must be {CONFIGURATION_IDS}")
    for configuration in result.values():
        if configuration["read_concern"] not in {"local", "majority"}:
            raise ValueError("read_concern must be local or majority")
        if configuration["write_concern"] not in {"w:1", "majority"}:
            raise ValueError("write_concern must be w:1 or majority")
        if not isinstance(configuration["causal_session"], bool):
            raise ValueError("causal_session must be boolean")
    return result


def load_predictions(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the prediction manifest used by analysis and report generation."""

    payload = load_json(path or CONFIG_ROOT / "predictions.json")
    predictions = payload.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError("predictions must be a list")
    result = {str(item["configuration_id"]): dict(item) for item in predictions}
    if tuple(result) != CONFIGURATION_IDS:
        raise ValueError(f"prediction IDs must be {CONFIGURATION_IDS}")
    for prediction in result.values():
        targets = prediction.get("guarantee_targets")
        if not isinstance(targets, list) or any(item not in PROPERTIES for item in targets):
            raise ValueError("guarantee_targets must contain known properties")
    return result
