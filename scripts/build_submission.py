"""Build the LaTeX report and a filtered, checksummed submission archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = Path("submission/metadata.mk")
PACKAGE_README_PATH = Path("submission/package-readme.md")
REQUIRED_SUBMISSION_FILES = (
    Path("submission/report.tex"),
    Path("submission/report.bib"),
    Path("submission/metadata.mk"),
    Path("submission/package-readme.md"),
)
REQUIRED_SECTIONS = tuple(
    Path("submission/sections") / f"{number:02d}-{name}.tex"
    for number, name in (
        (1, "abstract"),
        (2, "introduction"),
        (3, "background"),
        (4, "deployment"),
        (5, "predictions"),
        (6, "method"),
        (7, "results"),
        (8, "discussion"),
        (9, "limits"),
        (10, "reproduction"),
        (11, "conclusion"),
        (12, "tool-use"),
        (13, "appendix"),
    )
)
METADATA_KEYS = (
    "COURSE_CODE",
    "PROJECT_TITLE",
    "TEAM_NAME",
    "TEAM_MEMBERS",
    "SUBMISSION_DATE",
    "AI_USE_DISCLOSURE",
)
REPORT_FILENAME = "mongo-consistency-report.pdf"
ARCHIVE_FILENAME = "mongo-consistency-submission.zip"
PACKAGE_ROOT_FILES = (
    "Makefile",
    "README.md",
    "TODO.md",
    ".gitignore",
    ".dockerignore",
    "compose.yaml",
    "pyproject.toml",
    "requirements.txt",
)
PACKAGE_ROOT_DIRS = (
    "configs",
    "docs",
    "figures",
    "infra",
    "results",
    "schemas",
    "scripts",
    "src",
    "submission",
    "tests",
)
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "graphify-out",
        "node_modules",
        "output",
        "tmp",
        "venv",
        ".venv",
    }
)


class BuildError(RuntimeError):
    """A user-actionable submission build failure."""


def _remove_generated(path: Path) -> None:
    """Remove one exact generated path and refuse symlink targets."""

    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise BuildError(f"Refusing to remove symlink at generated path: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def parse_metadata(path: Path) -> dict[str, str]:
    """Parse the small, non-shell metadata file used by the report."""

    values: dict[str, str] = {}
    assignment = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*)")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise BuildError(f"Cannot read metadata file {path}: {error}") from error

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.fullmatch(stripped)
        if not match:
            raise BuildError(
                f"Invalid metadata assignment at {path}:{line_number}: {line!r}"
            )
        key, value = match.groups()
        values[key] = value.strip()

    missing = [key for key in METADATA_KEYS if not values.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise BuildError(f"Metadata fields are empty: {joined}")
    return values


def escape_latex(value: str) -> str:
    """Escape metadata for a generated LaTeX macro."""

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
    return re.sub(r"[\\&%$#_{}~^]", lambda match: replacements[match.group()], value)


def write_generated_metadata(path: Path, values: dict[str, str]) -> None:
    """Write build-only LaTeX macros from the metadata values."""

    macros = {
        "CourseCode": values["COURSE_CODE"],
        "ProjectTitle": values["PROJECT_TITLE"],
        "TeamName": values["TEAM_NAME"],
        "TeamMembers": values["TEAM_MEMBERS"],
        "SubmissionDate": values["SUBMISSION_DATE"],
        "AiUseDisclosure": values["AI_USE_DISCLOSURE"],
    }
    content = "\n".join(
        f"\\newcommand{{\\{name}}}{{{escape_latex(value)}}}"
        for name, value in macros.items()
    )
    path.write_text(content + "\n", encoding="utf-8")


def write_generated_analysis(path: Path, root: Path) -> None:
    """Write report macros from the latest offline summary, or explicit no-data."""

    summary_path = root / "results/summary/summary.json"
    status = "NO_DATA"
    history_count = 0
    outcome_counts = {
        "PASS": 0,
        "VIOLATION": 0,
        "UNAVAILABLE": 0,
        "INDETERMINATE": 0,
        "HARNESS_ERROR": 0,
        "UNSUPPORTED": 0,
    }
    overall: dict[str, object] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            status = str(summary.get("status", status))
            history_count = int(summary.get("history_count", 0) or 0)
            counts = summary.get("outcome_counts", {})
            if isinstance(counts, dict):
                for outcome in outcome_counts:
                    outcome_counts[outcome] = int(counts.get(outcome, 0) or 0)
            candidate = summary.get("overall", {})
            if isinstance(candidate, dict):
                overall = candidate

    def metric(path: tuple[str, ...]) -> str:
        value: object = overall
        for key in path:
            if not isinstance(value, dict):
                return "NO_DATA"
            value = value.get(key)
        if value is None:
            return "NO_DATA"
        if isinstance(value, (int, float)):
            return f"{value:.4f}"
        return str(value)

    macros = {
        "AnalysisStatus": status,
        "HistoryCount": str(history_count),
        **{f"{outcome.title().replace('_', '')}Count": str(count) for outcome, count in outcome_counts.items()},
        "ConsistencyViolationRate": metric(("consistency_violation_rate",)),
        "OperationSuccessRate": metric(("operation_success_rate",)),
        "HistoryCompletionRate": metric(("history_completion_rate",)),
        "LatencyMedian": metric(("latency_ms", "p50")),
        "LatencyHigh": metric(("latency_ms", "p95")),
        "LatencyTail": metric(("latency_ms", "p99")),
        "ElectionMedian": metric(("election_ms", "p50")),
        "RecoveryMedian": metric(("recovery_ms", "p50")),
    }
    content = "\n".join(
        f"\\newcommand{{\\{name}}}{{{escape_latex(value)}}}"
        for name, value in macros.items()
    )
    path.write_text(content + "\n", encoding="utf-8")


def validate_layout(root: Path) -> None:
    """Check the source layout before starting a build."""

    required = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        raise BuildError(f"Submission layout is incomplete; missing: {formatted}")


def _iter_files(root: Path, paths: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    """Yield source files and their repository-relative paths."""

    for relative in paths:
        source = root / relative
        if source.is_file() and not source.is_symlink():
            yield source, relative
            continue
        if not source.exists():
            continue
        if source.is_symlink():
            raise BuildError(f"Refusing to package symlink: {relative}")
        for candidate in sorted(source.rglob("*")):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            candidate_relative = candidate.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in candidate_relative.parts):
                continue
            if candidate.suffix in {".pyc", ".pyo"}:
                continue
            yield candidate, candidate_relative


def copy_source_tree(root: Path, destination: Path) -> list[Path]:
    """Copy project source while excluding local build metadata."""

    destination.mkdir(parents=True, exist_ok=True)
    paths = [Path(path) for path in PACKAGE_ROOT_FILES]
    paths.extend(Path(path) for path in PACKAGE_ROOT_DIRS)
    copied: list[Path] = []
    for source, relative in _iter_files(root, paths):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative)
    return sorted(copied)


def _command_tail(output: str, limit: int = 1200) -> str:
    compact = output.strip()
    return compact[-limit:] if compact else "no diagnostic output"


def texlive_bin_dirs() -> tuple[Path, ...]:
    """Return configured and user-local TeX Live binary directories."""

    candidates: list[Path] = []
    configured = os.environ.get("TEXLIVE_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    user_texlive = Path.home() / "texlive"
    if user_texlive.is_dir():
        for version in sorted(user_texlive.iterdir()):
            bin_root = version / "bin"
            if bin_root.is_dir():
                candidates.extend(
                    path for path in sorted(bin_root.iterdir()) if path.is_dir()
                )
    return tuple(dict.fromkeys(path for path in candidates if path.is_dir()))


def find_tool(name: str) -> str | None:
    """Find a tool on PATH or in a user-local TeX Live installation."""

    found = shutil.which(name)
    if found:
        return found
    for directory in texlive_bin_dirs():
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_command(command: list[str], cwd: Path, log_path: Path) -> None:
    """Run one build command and retain its complete output in a log."""

    environment = os.environ.copy()
    environment.setdefault("SOURCE_DATE_EPOCH", "0")
    command_directory = str(Path(command[0]).resolve().parent)
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if command_directory not in path_entries:
        environment["PATH"] = os.pathsep.join(
            (command_directory, environment.get("PATH", ""))
        )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"Command failed: {' '.join(command)}\n{error}") from error

    output = completed.stdout + completed.stderr
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(command)}\n{output}\n")
    if completed.returncode:
        raise BuildError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}\n"
            f"{_command_tail(output)}"
        )


def compile_report(root: Path, build_root: Path, values: dict[str, str]) -> Path:
    """Compile the report in an isolated temporary tree."""

    latex_root = build_root / "latex"
    source_target = latex_root / "submission"
    shutil.copytree(root / "submission", source_target)
    write_generated_metadata(source_target / "generated-metadata.tex", values)
    write_generated_analysis(source_target / "generated-analysis.tex", root)
    log_path = build_root / "latex-build.log"

    latexmk = find_tool("latexmk")
    engine = find_tool("pdflatex") or find_tool("xelatex") or find_tool("lualatex")
    bibtex = find_tool("bibtex")
    if latexmk and not bibtex:
        raise BuildError(
            "bibtex is required for this report. Install a TeX distribution and retry."
        )
    if latexmk:
        run_command(
            [
                latexmk,
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "submission/report.tex",
            ],
            latex_root,
            log_path,
        )
    elif engine:
        run_command(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "submission/report.tex",
            ],
            latex_root,
            log_path,
        )
        if not bibtex:
            raise BuildError(
                "bibtex is required when latexmk is unavailable. Install a TeX distribution and retry."
            )
        run_command([bibtex, "report"], latex_root, log_path)
        for _ in range(2):
            run_command(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "submission/report.tex",
                ],
                latex_root,
                log_path,
            )
    else:
        raise BuildError(
            "No LaTeX compiler was found. Install MacTeX or TeX Live with latexmk, "
            "pdflatex, and bibtex, then run make submission again."
        )

    report_path = latex_root / "report.pdf"
    if not report_path.is_file() or report_path.stat().st_size == 0:
        raise BuildError(f"LaTeX completed without a report PDF: {report_path}")
    return report_path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_revision(root: Path) -> str:
    """Return HEAD when available without making Git a build requirement."""

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


def write_manifest(package_root: Path, root: Path) -> Path:
    """Write checksums for every package payload except the manifest itself."""

    manifest = package_root / "manifest.txt"
    payloads = sorted(
        path for path in package_root.rglob("*") if path.is_file() and path != manifest
    )
    lines = [
        "MongoDB consistency submission package",
        f"Source revision: {source_revision(root)}",
        "Checksums: SHA-256",
        "",
    ]
    lines.extend(
        f"{sha256(path)}  {path.relative_to(package_root).as_posix()}"
        for path in payloads
    )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def make_archive(package_root: Path, archive_path: Path) -> None:
    """Create a stable zip archive with sorted entries and fixed timestamps."""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(path for path in package_root.rglob("*") if path.is_file()):
            relative = path.relative_to(package_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def check_documents(root: Path, checker_root: Path) -> None:
    """Run the repository checker against a source or package tree."""

    sys.path.insert(0, str(checker_root))
    try:
        from scripts.check_documentation import check_repository
    except ImportError as error:
        raise BuildError(f"Cannot load the documentation checker: {error}") from error

    files, findings = check_repository(root)
    if findings:
        details = "\n".join(finding.format() for finding in findings)
        raise BuildError(
            f"Documentation check failed for {len(findings)} finding(s) in {len(files)} file(s).\n{details}"
        )


def build(root: Path) -> tuple[Path, Path, Path]:
    """Build and validate the PDF, archive, and manifest."""

    root = root.resolve()
    validate_layout(root)
    values = parse_metadata(root / METADATA_PATH)

    output_pdf_dir = root / "output/pdf"
    output_submission_dir = root / "output/submission"
    build_root = root / "tmp/submission-build"
    for path in (output_pdf_dir, output_submission_dir, build_root):
        _remove_generated(path)
        path.mkdir(parents=True, exist_ok=True)

    check_documents(root, root)
    report_path = compile_report(root, build_root, values)

    package_root = build_root / "package"
    package_root.mkdir()
    shutil.copy2(report_path, package_root / "report.pdf")
    package_readme = root / PACKAGE_README_PATH
    shutil.copy2(package_readme, package_root / "README.md")
    source_root = package_root / "source"
    copy_source_tree(root, source_root)
    shutil.copy2(
        build_root / "latex/submission/generated-metadata.tex",
        source_root / "submission/generated-metadata.tex",
    )
    shutil.copy2(
        build_root / "latex/submission/generated-analysis.tex",
        source_root / "submission/generated-analysis.tex",
    )
    manifest = write_manifest(package_root, root)
    check_documents(package_root, root)

    final_pdf = output_pdf_dir / REPORT_FILENAME
    final_manifest = output_submission_dir / "manifest.txt"
    final_archive = output_submission_dir / ARCHIVE_FILENAME
    shutil.copy2(report_path, final_pdf)
    shutil.copy2(manifest, final_manifest)
    make_archive(package_root, final_archive)
    _remove_generated(build_root)
    return final_pdf, final_archive, final_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: directory containing this script)",
    )
    args = parser.parse_args(argv)
    try:
        pdf, archive, manifest = build(args.root)
    except BuildError as error:
        print(f"Submission build failed: {error}", file=sys.stderr)
        return 1
    print(f"Report: {pdf}")
    print(f"Archive: {archive}")
    print(f"Manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
