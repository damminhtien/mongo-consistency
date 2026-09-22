# Report structure

The report is a manually maintained LaTeX document in
[`../submission/report.tex`](../submission/report.tex). Its prose and table
structure are written in the checked-in `.tex` files. Analysis commands may
write numeric macros and table rows from saved results, but they do not write
report prose or sections. The structure follows the reader's questions, not
the order in which the project tasks happened.

## Sections

1. Introduction: the problem, two central questions, and the experimental cube.
2. Background and Expectations: the four properties, MongoDB mechanisms, C1-C8,
   and predictions.
3. Experimental Method: deployment, workloads, checkers, scenarios, outcomes,
   and metrics.
4. Results: baseline, configuration effects, fault effects, selected mechanism
   evidence, and client-visible completion consequences.
5. Discussion: three synthesis claims drawn across campaigns.
6. Reproducibility: commands and evidence locations.
7. Conclusion: the three findings that answer the two central questions.

The full configuration matrices, raw history identities, hashes, and CSV
metrics remain in the repository as audit material. They are not reproduced as
generated document fragments.

## Narrative rules

- Use only two high-level questions. Mechanism evidence and completion/latency
  analysis are supporting analyses, not independent research questions.
- Explain the property definitions and MongoDB mechanisms before presenting
  results.
- Describe the experiment as a property by configuration by scenario cube.
- Put prediction statements in Background and Expectations and method details
  in Experimental Method. Results should report what happened and why it
  matters without repeating the same template for every property.
- Put cross-campaign interpretation in Discussion. Do not repeat the same
  finding in Results, Discussion, and Conclusion.
- State scope qualifications beside the relevant result when omitting them
  would change the reader's interpretation.
- Keep raw JSON histories and machine-readable CSV summaries as the evidence
  boundary. Do not fabricate a value for an unavailable or indeterminate cell.

## Submission boundary

`make submission` builds two deliverables: the PDF compiled from the authored
LaTeX source and an archive containing that PDF plus the MongoDB runner,
Compose files, runtime configuration, schemas, and code needed to execute the
campaigns. The archive excludes `.tex` and bibliography sources, document
generators, tests, raw histories, and build output. `make
check-submission-artifacts` enforces that boundary.
