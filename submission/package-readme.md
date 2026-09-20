# MongoDB consistency submission package

The archive contains the report source, project documentation, executable
harness, tests, and the raw evidence available in the source revision used to
build it. Smoke histories are excluded from scientific evidence.

## Rebuild

From a checkout with the pinned Python environment and LaTeX tooling installed:

~~~bash
make test
make check-docs
make check-schemas
make analyse
make check-release-ready
make submission
~~~

The command writes
output/pdf/mongo-consistency-report.pdf,
output/submission/mongo-consistency-submission.zip, and a SHA-256 manifest.
The archive source is placed under source/ so the report can be rebuilt from
that directory.

## Evidence status

The report reads its counts and metrics from generated analysis artifacts. A
missing or incomplete campaign is labelled as such and is not presented as a
zero-violation result. The 32-history smoke is machinery validation and is not
included in the archive as scientific evidence. The pilot is kept separate
from RQ1 estimates. RQ1 is run sequentially. RQ2 has a separate manifest and
analysis condition: 384 core histories across C1/C3/C4/C6, F1/F2/F3, and
RYW/MR/MW/WFR, plus 48 partition-signature histories. The matching RQ1 normal
histories are the descriptive baseline; no normal histories are repeated in
RQ2. The 432 histories are grouped into 36 shared fault episodes, so report
episode-level election and recovery measures separately from history-level
outcomes.

## Metadata

Set the team name, member names and student IDs, submission date, and any
course-required AI disclosure in source/submission/metadata.mk before making
the final archive.
