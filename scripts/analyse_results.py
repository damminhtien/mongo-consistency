"""Rebuild offline summaries and figures without connecting to MongoDB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mongo_consistency.analysis import analyse

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=ROOT / "results/raw")
    parser.add_argument("--summary-root", type=Path, default=ROOT / "results/summary")
    parser.add_argument("--figures-root", type=Path, default=ROOT / "figures")
    parser.add_argument(
        "--submission-figures-root",
        type=Path,
        default=ROOT / "submission/figures",
    )
    args = parser.parse_args()
    summary = analyse(
        raw_root=args.raw_root,
        summary_root=args.summary_root,
        figures_root=args.figures_root,
        submission_figures_root=args.submission_figures_root,
    )
    print(json.dumps({"status": summary["status"], "history_count": summary["history_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
