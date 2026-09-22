from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from scripts.build_submission import (
    METADATA_KEYS,
    PACKAGE_ROOT_DIRS,
    PACKAGE_ROOT_FILES,
    PACKAGE_RUNTIME_SCRIPTS,
    REQUIRED_SECTIONS,
    REQUIRED_SUBMISSION_FILES,
    BuildError,
    copy_source_tree,
    parse_metadata,
    validate_figure_inputs,
    validate_package_sources,
    validate_pdf_text,
    write_generated_analysis,
)
from scripts.package_provenance import packaged_revision, packaged_tree_is_clean

ROOT = Path(__file__).resolve().parents[1]


class SubmissionLayoutTests(unittest.TestCase):
    def test_required_submission_sources_exist(self) -> None:
        paths = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
        missing = [str(path) for path in paths if not (ROOT / path).is_file()
        ]
        self.assertEqual([], missing)

    def test_metadata_remains_a_validated_input(self) -> None:
        values = parse_metadata(ROOT / "submission/metadata.mk")
        self.assertEqual(set(METADATA_KEYS), set(values))
        self.assertTrue(all(values[key] for key in METADATA_KEYS))

    def test_cover_is_authored_directly_in_report_source(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        cover = report.split(r"\begin{titlepage}", maxsplit=1)[1].split(
            r"\end{titlepage}", maxsplit=1
        )[0]
        self.assertIn(r"\mbox{Consistency Models in Distributed Databases}", cover)
        self.assertIn("Dam Minh Tien", cover)
        self.assertIn("A0355091E", cover)
        self.assertIn("damminhtien@u.nus.edu", cover)
        self.assertIn("Project supervisor: Zhenning Cai", cover)
        self.assertNotIn("Email:", cover)
        self.assertNotIn(r"\ProjectTitle", cover)
        self.assertNotIn(r"\ProjectSupervisor", cover)
        self.assertNotIn(r"\includegraphics", cover)
        self.assertNotIn("Examiner", cover)

    def test_front_matter_has_requested_list_boundaries(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        figures = report.index(r"\listoffigures")
        contents = report.index(r"\tableofcontents")
        tables = report.index(r"\listoftables")
        self.assertLess(figures, contents)
        self.assertLess(contents, tables)
        self.assertRegex(report[figures:contents], r"\s*\\clearpage\s*")
        self.assertNotIn(r"\clearpage", report[tables:report.index(r"\input{submission/sections/00-abbreviations.tex")])

        abbreviations = (ROOT / "submission/sections/00-abbreviations.tex").read_text(
            encoding="utf-8"
        )
        for abbreviation in ("C1-C8", "F1-F3", "MR", "MW", "RC", "RYW", "WC", "WFR"):
            self.assertIn(f"{abbreviation} &", abbreviations)
        listed = {
            line.split(" & ", 1)[0]
            for line in abbreviations.splitlines()
            if " & " in line
        }
        self.assertTrue({"AI", "AY", "DSA5208", "ID", "NUS", "PDF", "RQ1/RQ2", "SHA-256"}.isdisjoint(listed))
        self.assertIn("eight combinations of read concern", abbreviations)

    def test_sources_are_in_reader_order_and_have_two_questions(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        ordered = (
            r"\input{submission/sections/02-introduction.tex}",
            r"\input{submission/sections/03-background.tex}",
            r"\input{submission/sections/04-method.tex}",
            r"\input{submission/sections/07-results.tex}",
            r"\input{submission/sections/08-discussion.tex}",
            r"\input{submission/sections/10-reproduction.tex}",
            r"\input{submission/sections/11-conclusion.tex}",
        )
        positions = [report.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn(r"\input{submission/sections/09-limits.tex}", report)
        self.assertFalse((ROOT / "submission/sections/09-limits.tex").exists())

        introduction = (ROOT / "submission/sections/02-introduction.tex").read_text(encoding="utf-8")
        self.assertIn("Q_1", introduction)
        self.assertIn("Q_2", introduction)
        self.assertNotIn("RQ3", introduction)
        self.assertNotIn("RQ4", introduction)

        for source_name in ("07-results.tex", "08-discussion.tex", "11-conclusion.tex"):
            source = (ROOT / "submission/sections" / source_name).read_text(encoding="utf-8")
            self.assertNotIn(r"\section{RQ3}", source)
            self.assertNotIn(r"\section{RQ4}", source)

    def test_course_definitions_are_separate_from_project_proxies(self) -> None:
        background = (ROOT / "submission/sections/03-background.tex").read_text(
            encoding="utf-8"
        )
        normalized_background = " ".join(background.split())
        self.assertIn(r"\paragraph{Course definitions.}", background)
        self.assertIn(r"\paragraph{Operationalisation in this project.}", background)
        self.assertIn(
            r"effect of a write operation by a process on data item \(x\) will "
            r"always be seen by a successive read operation on \(x\)",
            normalized_background,
        )
        self.assertIn(
            r"reads the value of a data item \(x\), any successive read operation "
            r"on \(x\) by that process will always return that same value or a more "
            r"recent value",
            normalized_background,
        )
        self.assertIn(
            r"completed before any successive write operation",
            normalized_background,
        )
        self.assertIn(
            r"following a previous read operation on \(x\) by the same process "
            r"is guaranteed to take place on the same or a more recent value",
            normalized_background,
        )
        self.assertIn("atomic pre-image", background)
        self.assertNotIn(
            "A successor write must not become visible without its predecessor", background
        )
        self.assertNotIn("not become visible without that version", background)

        method = (ROOT / "submission/sections/06-method.tex").read_text(encoding="utf-8")
        self.assertIn("course definitions in Section~2.1", method)
        self.assertNotIn("project operationalisation in\nSection~2.1", method)

        results = (ROOT / "submission/sections/07-results.tex").read_text(encoding="utf-8")
        self.assertIn("atomic pre-image", results)
        self.assertIn("post-heal witness is used only", results)

        protocol = (ROOT / "docs/experimental-protocol.md").read_text(encoding="utf-8")
        self.assertIn("## Course definitions and project operationalisation", protocol)
        self.assertIn("write_base_version", protocol)
        self.assertIn("direct MW checker reports PASS", protocol)
        self.assertIn("direct WFR checker reports VIOLATION", protocol)

    def test_professor_slides_are_cited_and_web_references_have_urls(self) -> None:
        bibliography = (ROOT / "submission/report.bib").read_text(encoding="utf-8")
        sections = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "submission/sections").glob("*.tex")
        )
        self.assertIn("@misc{cai-lecture-1,", bibliography)
        self.assertIn("@misc{cai-lecture-3,", bibliography)
        entries = {
            match.group(1): match.group(2)
            for match in re.finditer(r"(?ms)^@\w+\{([^,]+),(.*?)^\}", bibliography)
        }
        cited = {
            key.strip()
            for citation in re.findall(r"\\cite(?:\[[^\]]*\])?\{([^}]+)\}", sections)
            for key in citation.split(",")
        }
        self.assertTrue(cited)
        self.assertEqual(set(), cited - entries.keys())
        for key in sorted(cited - {"cai-lecture-1", "cai-lecture-3"}):
            self.assertRegex(entries[key], r"(?m)^\s*url\s*=\s*\{https?://[^}]+\}")

    def test_makefile_and_workflows_build_the_pdf_submission(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("check-submission-artifacts", makefile)
        self.assertIn("scripts/build_submission.py", makefile)
        self.assertNotIn("package_submission.py", makefile)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("latexmk", workflow)
        self.assertIn("mongo-consistency-report.pdf", workflow)
        self.assertIn("mongo-consistency-submission.zip", workflow)

    def test_submission_allowlist_excludes_report_source_and_tests(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"configs"', source)
        self.assertIn('"src"', source)
        self.assertNotIn("scripts/analyse_results.py", PACKAGE_RUNTIME_SCRIPTS)
        self.assertNotIn("scripts/analyse_rq3.py", PACKAGE_RUNTIME_SCRIPTS)
        self.assertNotIn("scripts/analyse_rq4.py", PACKAGE_RUNTIME_SCRIPTS)
        self.assertNotIn('"submission"', "\n".join(PACKAGE_ROOT_DIRS))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("compose.yaml", "src/mongo_consistency/trial.py", "tests/test_x.py", "submission/report.tex"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder\n", encoding="utf-8")
            copied = copy_source_tree(root, root / "package")
        copied_paths = {path.as_posix() for path in copied}
        self.assertIn("compose.yaml", copied_paths)
        self.assertNotIn("tests/test_x.py", copied_paths)
        self.assertNotIn("submission/report.tex", copied_paths)

    def test_submission_runtime_sources_and_commands_are_complete(self) -> None:
        validate_package_sources(ROOT)
        package_makefile = (ROOT / "submission/Makefile").read_text(encoding="utf-8")
        self.assertIn("SOURCE_DIR ?= source", package_makefile)
        for target in ("experiment", "rq2", "rq3"):
            self.assertIn(f"{target}:", package_makefile)
        self.assertNotIn("analyse:", package_makefile)
        self.assertNotIn("analyse_rq4.py", package_makefile)
        with tempfile.TemporaryDirectory() as directory:
            copied = copy_source_tree(ROOT, Path(directory) / "source")
        copied_paths = {path.as_posix() for path in copied}
        required_files = {
            *PACKAGE_ROOT_FILES,
            *PACKAGE_RUNTIME_SCRIPTS,
        }
        self.assertTrue(required_files.issubset(copied_paths))

    def test_submission_runtime_sources_fail_closed_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            BuildError, "runtime source is incomplete"
        ):
            validate_package_sources(Path(directory))

    def test_extracted_package_uses_manifest_provenance_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory)
            source_root = package_root / "source"
            source_root.mkdir()
            (package_root / "manifest.txt").write_text(
                "Source revision: abc123\nWorking tree state: clean\n",
                encoding="utf-8",
            )
            self.assertEqual("abc123", packaged_revision(source_root))
            self.assertTrue(packaged_tree_is_clean(source_root))

    def test_submission_build_rejects_missing_figure_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "submission/sections/report.tex"
            source.parent.mkdir(parents=True)
            source.write_text(r"\maybefigure{submission/figures/missing.pdf}{Missing figure}", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "Missing report figure input"):
                validate_figure_inputs(root)
            figure = root / "submission/figures/missing.pdf"
            figure.parent.mkdir(parents=True)
            figure.write_bytes(b"placeholder fixture")
            validate_figure_inputs(root)

    def test_compiled_pdf_rejects_unresolved_data_placeholders(self) -> None:
        validate_pdf_text("A complete report contains no unresolved placeholders.")
        with self.assertRaisesRegex(BuildError, "NO DATA placeholder"):
            validate_pdf_text("Figure box: NO DATA: missing figure")
        with self.assertRaisesRegex(BuildError, "NO DATA placeholder"):
            validate_pdf_text("Table row: NO_DATA")

    def test_generated_analysis_contains_data_macros_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "generated-analysis.tex"
            write_generated_analysis(target, Path(directory))
            content = target.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\AnalysisStatus}{NO\_DATA}", content)
        self.assertIn(r"\newcommand{\LatencyMedian}{NO\_DATA}", content)
        self.assertNotIn("This report", content)


if __name__ == "__main__":
    unittest.main()
