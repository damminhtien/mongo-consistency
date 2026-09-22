from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from scripts.build_submission import (
    METADATA_KEYS,
    REQUIRED_SECTIONS,
    REQUIRED_SUBMISSION_FILES,
    parse_metadata,
    write_generated_analysis,
    write_generated_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


class SubmissionLayoutTests(unittest.TestCase):
    def test_required_submission_sources_exist(self) -> None:
        paths = (*REQUIRED_SUBMISSION_FILES, *REQUIRED_SECTIONS)
        missing = [str(path) for path in paths if not (ROOT / path).is_file()]
        self.assertEqual([], missing)

    def test_metadata_has_all_build_fields(self) -> None:
        values = parse_metadata(ROOT / "submission/metadata.mk")
        self.assertEqual(set(METADATA_KEYS), set(values))
        self.assertTrue(all(values[key] for key in METADATA_KEYS))

    def test_cover_metadata_is_written_to_latex_macros(self) -> None:
        from tempfile import TemporaryDirectory

        values = parse_metadata(ROOT / "submission/metadata.mk")
        with TemporaryDirectory() as directory:
            target = Path(directory) / "generated-metadata.tex"
            write_generated_metadata(target, values)
            generated = target.read_text(encoding="utf-8")

        self.assertIn(
            r"\newcommand{\ProjectTitle}{Consistency Models in Distributed Databases}",
            generated,
        )
        self.assertIn(
            r"\newcommand{\CourseTitle}{Scalable Distributed Computing for Data Science}",
            generated,
        )
        self.assertIn(
            r"\newcommand{\ProjectSupervisor}{Prof. Zhenning Cai}", generated
        )
        self.assertIn(r"\newcommand{\AcademicYear}{AY2026/2027}", generated)
        self.assertIn(r"\newcommand{\TeamMembers}{Dam Minh Tien\\", generated)
        self.assertIn("A0355091E\\\\\n\\StudentEmail", generated)
        self.assertNotIn(r"Email: \StudentEmail", generated)
        self.assertLess(
            generated.index(r"\StudentEmail"),
            generated.index("Nguyen Minh Duc"),
        )
        self.assertIn(r"\newcommand{\StudentEmail}{damminhtien@u.nus.edu}", generated)

    def test_cover_matches_course_project_layout(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        cover = report.split(r"\begin{titlepage}", maxsplit=1)[1].split(
            r"\end{titlepage}", maxsplit=1
        )[0]
        self.assertIn(r"\begin{titlepage}", report)
        self.assertIn(r"\newgeometry{margin=3cm}", report)
        self.assertIn(r"\restoregeometry", report)
        self.assertIn(r"\centering", cover)
        self.assertIn(r"\fontsize{18}{24}", cover)
        self.assertIn(r"\fontsize{12}{18}", cover)
        self.assertIn(r"\fontfamily{ptm}", cover)
        self.assertNotIn(r"\vspace*{0.10\textheight}", cover)
        self.assertIn(r"\CourseCode{}: PROJECT 1", cover)
        self.assertNotIn(r"Email: \StudentEmail", cover)
        self.assertIn(r"Project supervisor: \ProjectSupervisor", cover)
        self.assertIn(r"\CourseCode{} \CourseTitle", cover)
        self.assertIn("Centre for Data Science and Machine Learning", cover)
        self.assertIn("Department of Mathematics", cover)
        self.assertIn("National University of Singapore", cover)
        self.assertIn("Project 1, Semester 1, \\AcademicYear", cover)
        self.assertNotIn(r"\MakeUppercase", cover)
        self.assertNotIn(r"\includegraphics", cover)
        self.assertNotIn(r"\href", cover)
        self.assertNotIn("Examiner", cover)
        self.assertNotIn(r"\section*{Submission status}", report)
        self.assertNotIn("The report includes measured evidence only", report)
        self.assertNotIn(
            "Complete pending course metadata before the Canvas upload", report
        )

    def test_professor_lecture_slides_are_bibliographic_sources(self) -> None:
        bibliography = (ROOT / "submission/report.bib").read_text(encoding="utf-8")
        introduction = (ROOT / "submission/sections/02-introduction.tex").read_text(
            encoding="utf-8"
        )
        background = (ROOT / "submission/sections/03-background.tex").read_text(
            encoding="utf-8"
        )
        configurations = (ROOT / "submission/sections/05-predictions.tex").read_text(
            encoding="utf-8"
        )

        self.assertIn("@misc{cai-lecture-1,", bibliography)
        self.assertIn("@misc{cai-lecture-3,", bibliography)
        self.assertIn(r"\cite[slides 37 to 44]{cai-lecture-3}", introduction)
        self.assertIn(r"\cite[slides 17 to 20]{cai-lecture-1}", background)
        self.assertIn(r"\cite[slides 49 to 53]{cai-lecture-3}", configurations)

    def test_cited_web_references_have_urls_and_are_rendered(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        bibliography = (ROOT / "submission/report.bib").read_text(encoding="utf-8")
        sections = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "submission/sections").glob("*.tex")
        )

        entries = {
            match.group(1): match.group(2)
            for match in re.finditer(r"(?ms)^@\w+\{([^,]+),(.*?)^\}", bibliography)
        }
        cited_keys = {
            key.strip()
            for citation in re.findall(r"\\cite(?:\[[^\]]*\])?\{([^}]+)\}", sections)
            for key in citation.split(",")
        }

        self.assertTrue(cited_keys)
        self.assertEqual(set(), cited_keys - entries.keys())
        self.assertIn(r"\bibliographystyle{plainurl}", report)
        course_sources = {"cai-lecture-1", "cai-lecture-3"}
        for key in sorted(cited_keys - course_sources):
            self.assertRegex(
                entries[key],
                r"(?m)^\s*url\s*=\s*\{https?://[^}]+\}",
                msg=f"Cited web reference {key} must define a URL",
            )

    def test_front_matter_lists_figures_tables_and_abbreviations(self) -> None:
        report = (ROOT / "submission/report.tex").read_text(encoding="utf-8")
        acknowledgements = (
            ROOT / "submission/sections/00-acknowledgements.tex"
        ).read_text(encoding="utf-8")
        abbreviations = (ROOT / "submission/sections/00-abbreviations.tex").read_text(
            encoding="utf-8"
        )
        ordered = (
            r"\pagenumbering{roman}",
            r"\input{submission/sections/01-abstract.tex}",
            r"\input{submission/sections/00-acknowledgements.tex}",
            r"\addcontentsline{toc}{section}{Contents}",
            r"\listoffigures",
            r"\listoftables",
            r"\input{submission/sections/00-abbreviations.tex}",
            r"\pagenumbering{arabic}",
            r"\input{submission/sections/02-introduction.tex}",
        )
        positions = [report.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(r"\section*{Acknowledgements}", acknowledgements)
        self.assertIn("We thank Prof. Zhenning Cai", acknowledgements)
        self.assertIn(r"\begin{tabular}", abbreviations)
        self.assertNotIn(r"\begin{longtable}", abbreviations)

        abstract_input = r"\input{submission/sections/01-abstract.tex}"
        acknowledgements_input = r"\input{submission/sections/00-acknowledgements.tex}"
        abbreviations_input = r"\input{submission/sections/00-abbreviations.tex}"
        self.assertRegex(
            report,
            re.escape(abstract_input) + r"\s*" + re.escape(acknowledgements_input),
        )
        contents_end = report.index(r"\tableofcontents") + len(r"\tableofcontents")
        figures_start = report.index(r"\addcontentsline{toc}{section}{List of Figures}")
        self.assertRegex(
            report[contents_end:figures_start],
            r"\s*\\clearpage\s*",
        )
        lists_start = figures_start
        abbreviations_end = report.index(abbreviations_input) + len(abbreviations_input)
        self.assertNotIn(r"\clearpage", report[lists_start:abbreviations_end])
        self.assertRegex(
            report,
            re.escape(abbreviations_input)
            + r"\s*\\clearpage\s*\\pagenumbering\{arabic\}",
        )

        for abbreviation in (
            "C1-C8",
            "F1-F3",
            "H2.1-H2.4",
            "HTTP",
            "MR",
            "MW",
            "RC",
            "RQ",
            "RYW",
            "WC",
            "WFR",
        ):
            self.assertIn(f"{abbreviation} &", abbreviations)

        listed_abbreviations = {
            line.split(" & ", 1)[0]
            for line in abbreviations.splitlines()
            if " & " in line
        }
        self.assertTrue(
            {
                "AI",
                "AY",
                "DSA5208",
                "ID",
                "NUS",
                "PDF",
                "RQ1/RQ2",
                "SHA-256",
            }.isdisjoint(listed_abbreviations)
        )
        self.assertIn(
            "C denotes a configuration",
            abbreviations,
        )
        self.assertIn(
            "read concern, write concern, and causal-session setting",
            abbreviations,
        )
        self.assertIn("hypotheses in the second experiment", abbreviations)

        abstract = (ROOT / "submission/sections/01-abstract.tex").read_text(
            encoding="utf-8"
        )
        introduction = (ROOT / "submission/sections/02-introduction.tex").read_text(
            encoding="utf-8"
        )
        predictions = (ROOT / "submission/sections/05-predictions.tex").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("C1-C8", abstract)
        self.assertNotIn("RQ1", abstract)
        self.assertNotIn("RQ2", abstract)
        self.assertIn("eight configurations labelled C1", introduction)
        self.assertIn("RQ2 fault predictions", predictions)
        for hypothesis in ("H2.1", "H2.2", "H2.3", "H2.4"):
            self.assertIn(rf"\item[{hypothesis}]", predictions)

    def test_report_sections_cover_project_requirements_in_order(self) -> None:
        ordered_sections = (
            "02-introduction.tex",
            "03-background.tex",
            "04-deployment.tex",
            "05-predictions.tex",
            "06-method.tex",
            "07-results.tex",
            "08-discussion.tex",
            "09-limits.tex",
            "10-reproduction.tex",
            "11-conclusion.tex",
        )
        source = "\n".join(
            (ROOT / "submission/sections" / filename).read_text(encoding="utf-8")
            for filename in ordered_sections
        )
        expected_headings = (
            r"\section{Introduction}",
            r"\section{Database System and Deployment}",
            r"\section{Consistency Configurations}",
            r"\section{Experimental Design}",
            r"\section{Experiments and Results}",
            r"\section{Discussion}",
            r"\section{Limitations and Threats to Validity}",
            r"\section{Reproducibility}",
            r"\section{Conclusion}",
        )
        positions = [source.index(heading) for heading in expected_headings]
        self.assertEqual(positions, sorted(positions))

        results = (ROOT / "submission/sections/07-results.tex").read_text(
            encoding="utf-8"
        )
        for property_name in (
            "Read-your-writes consistency",
            "Monotonic-reads consistency",
            "Monotonic-writes consistency",
            "Writes-follow-reads consistency",
        ):
            self.assertIn(rf"\subsection{{{property_name}}}", results)
        property_sections = {
            "Read-your-writes consistency": ("predict", "schedule", "PASS", "VIOLATION"),
            "Monotonic-reads consistency": ("predict", "schedule", "PASS", "VIOLATION"),
            "Monotonic-writes consistency": ("predict", "schedule", "PASS", "VIOLATION"),
            "Writes-follow-reads consistency": ("predict", "schedule", "PASS", "VIOLATION"),
        }
        for property_name, terms in property_sections.items():
            start = results.index(rf"\subsection{{{property_name}}}")
            next_section = results.find("\n\\subsection{", start + 1)
            body = results[start:] if next_section == -1 else results[start:next_section]
            for term in terms:
                self.assertRegex(body, rf"(?i){re.escape(term)}")

        discussion = (ROOT / "submission/sections/08-discussion.tex").read_text(
            encoding="utf-8"
        )
        for heading in (
            "Predictions and observations",
            "Effect of consistency configuration",
            "Effect of node failure",
            "Effect of network partition",
            "Consistency and availability",
            "Unexpected behavior and interpretation",
        ):
            self.assertIn(rf"\subsection{{{heading}}}", discussion)

    def test_makefile_exposes_submission_target(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(".PHONY:", makefile)
        self.assertIn("check-docs", makefile)
        self.assertIn("check-generated:", makefile)
        self.assertIn("setup", makefile)
        self.assertIn("submission:", makefile)
        self.assertIn("scripts/build_submission.py", makefile)

    def test_source_manifest_has_explicit_package_roots(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"configs"', source)
        self.assertIn('"submission"', source)
        self.assertIn('"output"', source)
        self.assertIn('"tmp"', source)

    def test_submission_package_includes_runtime_harness_sources(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        for path in ("compose.yaml", "requirements.txt", "pyproject.toml", "infra"):
            self.assertIn(path, source)

    def test_generated_analysis_macros_show_no_data_without_summary(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            target = Path(directory) / "generated-analysis.tex"
            write_generated_analysis(target, Path(directory))
            content = target.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\AnalysisStatus}{NO\_DATA}", content)
        self.assertIn(r"\newcommand{\LatencyMedian}{NO\_DATA}", content)
        self.assertIn(r"\newcommand{\AdversarialViolationCount}{--}", content)
        self.assertIn(r"\newcommand{\RQTwoFaultEpisodeCount}{--}", content)

    def test_complete_rq2_keeps_rq1_observation_rows_macro(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "results/summary/summary.json"
            summary_path.parent.mkdir(parents=True)
            faults = ("F1", "F2", "F3")
            configurations = ("C1", "C3", "C4", "C6")
            properties = ("RYW", "MR", "MW", "WFR")
            summary = {
                "status": "DATA",
                "groups": [
                    {
                        "campaign_id": "rq2",
                        "topology_condition": fault,
                        "configuration_id": configuration,
                        "property": property_name,
                    }
                    for fault in faults
                    for configuration in configurations
                    for property_name in properties
                ],
                "fault_episode_summaries": [
                    {"topology_condition": fault, "episode_count": 1}
                    for fault in faults
                ],
            }
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            manifest_path = root / "results/raw/rq2/campaign-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text('{"status":"COMPLETE"}', encoding="utf-8")
            target = root / "generated-analysis.tex"
            write_generated_analysis(target, root)
            generated = target.read_text(encoding="utf-8")

        self.assertIn(r"\newcommand{\RQOnePredictionObservationRows}{", generated)
        self.assertIn(r"\newcommand{\RQTwoFaultEpisodeCount}{3}", generated)

    def test_generated_report_uses_main_campaign_and_separates_conditions(self) -> None:
        from tempfile import TemporaryDirectory

        summary = {
            "status": "DATA",
            "history_count": 1472,
            "overall": {"consistency_violation_rate": 0.99},
            "predictions": {"C1": {"guarantee_targets": ["RYW"]}},
            "groups": [
                {
                    "campaign_id": "experiment",
                    "adversarial": True,
                    "configuration_id": "C1",
                    "property": "RYW",
                    "history_count": 30,
                    "outcomes": {"PASS": 4, "VIOLATION": 20, "UNAVAILABLE": 6},
                }
            ],
            "campaign_summaries": {
                "pilot": {"history_count": 192},
                "experiment": {
                    "history_count": 1280,
                    "normal": {
                        "history_count": 320,
                        "outcomes": {"PASS": 264, "VIOLATION": 2},
                        "consistency_violation_rate": 2 / 266,
                        "operation_success_rate": 0.9,
                        "history_completion_rate": 0.99,
                        "latency_ms": {"p50": 1.0, "p95": 5.0, "p99": 10.0},
                        "election_ms": {"p50": None},
                        "recovery_ms": {"p50": None},
                    },
                    "adversarial": {
                        "history_count": 960,
                        "outcomes": {
                            "PASS": 434,
                            "VIOLATION": 6,
                            "PRECONDITION_MISS": 449,
                        },
                        "consistency_violation_rate": 6 / 440,
                        "operation_success_rate": 0.8,
                        "history_completion_rate": 0.5,
                        "latency_ms": {"p50": 2.0, "p95": 20.0, "p99": 100.0},
                        "election_ms": {"p50": 12000.0},
                        "recovery_ms": {"p50": 200.0},
                    },
                    "properties": {
                        "RYW": {
                            "history_count": 240,
                            "outcomes": {"PASS": 109, "VIOLATION": 6, "PRECONDITION_MISS": 111},
                        },
                        "MR": {
                            "history_count": 240,
                            "outcomes": {"PASS": 97},
                        },
                        "MW": {
                            "history_count": 240,
                            "outcomes": {"PASS": 104},
                        },
                        "WFR": {
                            "history_count": 240,
                            "outcomes": {"PASS": 124},
                        },
                    },
                },
            },
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "results/summary/summary.json"
            summary_path.parent.mkdir(parents=True)
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            manifest_path = root / "results/raw/experiment/campaign-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps({"status": "COMPLETE"}),
                encoding="utf-8",
            )
            target = root / "generated-analysis.tex"
            write_generated_analysis(target, root)
            content = target.read_text(encoding="utf-8")
        self.assertIn(r"\newcommand{\HistoryCount}{1280}", content)
        self.assertIn(r"\newcommand{\PilotHistoryCount}{192}", content)
        self.assertIn(r"\newcommand{\NormalViolationCount}{2}", content)
        self.assertIn(r"\newcommand{\AdversarialPreconditionMissCount}{449}", content)
        self.assertIn(r"\newcommand{\ConsistencyViolationRate}{0.0136}", content)
        self.assertIn(r"\newcommand{\MainCampaignStatus}{COMPLETE}", content)
        self.assertNotIn("ParallelWorkerCount", content)
        self.assertIn(r"\newcommand{\RYWViolationCount}{6}", content)
        self.assertIn(r"\newcommand{\RYWDecidableCount}{115}", content)
        self.assertIn(r"\newcommand{\RYWPreconditionMissCount}{111}", content)
        self.assertIn(r"\newcommand{\MRViolationCount}{0}", content)
        self.assertIn(r"\newcommand{\MRDecidableCount}{97}", content)
        self.assertIn(r"\newcommand{\RQOnePredictionObservationRows}{C1 & G; P=4 V=20 U=6", content)
        self.assertNotIn(r"\newcommand{\ConsistencyViolationRate}{0.9900}", content)

    def test_schema_contracts_are_packaged(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        self.assertIn('"schemas"', source)
        self.assertTrue((ROOT / "schemas/history.v1.json").is_file())

    def test_smoke_histories_are_not_packaged_as_submission_evidence(self) -> None:
        source = (ROOT / "scripts/build_submission.py").read_text(encoding="utf-8")
        for path in (
            "results/archive",
            "results/smoke",
            "results/smoke-dport",
            "results/smoke-verified",
        ):
            self.assertIn(f'Path("{path}")', source)

    def test_release_workflow_publishes_all_submission_outputs(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        for artifact in (
            "output/pdf/mongo-consistency-report.pdf",
            "output/submission/mongo-consistency-submission.zip",
            "output/submission/manifest.txt",
        ):
            self.assertIn(artifact, workflow)
        steps = [
            workflow.index("make analyse"),
            workflow.index("make check-release-ready"),
            workflow.index("make submission"),
        ]
        self.assertEqual(sorted(steps), steps)

    def test_ci_rebuilds_analysis_before_submission(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertLess(workflow.index("make analyse"), workflow.index("make submission"))
        self.assertLess(workflow.index("make rq3-analyse"), workflow.index("make check-generated"))
        self.assertLess(workflow.index("make rq4-analyse"), workflow.index("make check-generated"))

    def test_source_package_includes_raw_campaign_evidence(self) -> None:
        from tempfile import TemporaryDirectory

        from scripts.build_submission import copy_source_tree

        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "results/raw/experiment/history.json"
            history.parent.mkdir(parents=True)
            history.write_text("{}", encoding="utf-8")
            excluded_results = (
                "results/archive/attempt/history.json",
                "results/smoke/history.json",
                "results/smoke-dport/smoke/history.json",
                "results/smoke-verified/smoke/history.json",
            )
            for relative in excluded_results:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            copied = copy_source_tree(root, root / "package")
        copied_paths = {path.as_posix() for path in copied}
        self.assertIn("results/raw/experiment/history.json", copied_paths)
        for relative in excluded_results:
            self.assertNotIn(relative, copied_paths)


if __name__ == "__main__":
    unittest.main()
