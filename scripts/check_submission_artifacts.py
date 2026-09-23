"""Validate the PDF and archive produced by make submission."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from build_submission import (
    ARCHIVE_FILENAME,
    PACKAGE_RUNTIME_SCRIPTS,
    REPORT_FILENAME,
    BuildError,
    validate_pdf_artifact,
)

ROOT = Path(__file__).resolve().parents[1]


def check_artifacts(root: Path = ROOT) -> list[str]:
    """Return submission artifact failures without rebuilding the report."""

    errors: list[str] = []

    pdf_path = root / "output/pdf" / REPORT_FILENAME
    try:
        validate_pdf_artifact(pdf_path)
    except BuildError as error:
        errors.append(str(error))

    archive_path = root / "output/submission" / ARCHIVE_FILENAME
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        errors.append(f"Submission archive is missing or empty: {archive_path}")
    else:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                broken = archive.testzip()
                if broken is not None:
                    errors.append(f"Submission archive has a corrupt entry: {broken}")
                names = set(archive.namelist())
                for required in ("report.pdf", "README.md", "Makefile", "manifest.txt"):
                    if required not in names:
                        errors.append(f"Submission archive is missing {required}")
                required_source_files = (
                    "source/compose.yaml",
                    "source/pyproject.toml",
                    "source/requirements.txt",
                    "source/docs/experimental-protocol.md",
                    "source/infra/runner/Dockerfile",
                    "source/infra/fault-controller/Dockerfile",
                    *(f"source/{path}" for path in PACKAGE_RUNTIME_SCRIPTS),
                )
                for required in required_source_files:
                    if required not in names:
                        errors.append(f"Submission archive is missing {required}")
                forbidden_suffixes = (".tex", ".ltx", ".bib", ".cls", ".aux", ".log", ".pdf")
                forbidden = sorted(
                    name
                    for name in names
                    if (name.endswith(forbidden_suffixes) and name != "report.pdf")
                    or name.startswith(("tests/", "source/submission/", "source/scripts/analyse_"))
                    or name == "source/scripts/build_submission.py"
                    or name in {
                        "source/src/mongo_consistency/analysis.py",
                        "source/src/mongo_consistency/figures.py",
                        "source/src/mongo_consistency/rq4.py",
                    }
                )
                if forbidden:
                    errors.append(
                        "Submission archive contains excluded report or test files: "
                        + ", ".join(forbidden)
                    )
                if not any(name.startswith("source/configs/") for name in names):
                    errors.append("Submission archive is missing experiment configs under source/configs/")
                if not any(name.startswith("source/schemas/") for name in names):
                    errors.append("Submission archive is missing record schemas under source/schemas/")
                if not any(name.startswith("source/src/") for name in names):
                    errors.append("Submission archive is missing runtime source under source/src/")
        except (OSError, zipfile.BadZipFile) as error:
            errors.append(f"Cannot inspect submission archive: {error}")

    manifest = root / "output/submission/manifest.txt"
    if not manifest.is_file() or manifest.stat().st_size == 0:
        errors.append(f"Submission manifest is missing or empty: {manifest}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors = check_artifacts(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Submission artifacts passed: PDF, runtime-code archive, and manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
