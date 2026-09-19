# MongoDB consistency submission package

The package contains the report PDF, the LaTeX source, the repository documents, and the scripts and tests available in the checkout used for the build.

## Rebuild

From a checkout with a LaTeX engine installed:

```bash
make submission
```

The command writes `output/pdf/mongo-consistency-report.pdf`, `output/submission/mongo-consistency-submission.zip`, and a SHA-256 manifest. The archive source is under `source/` so that `make -C source submission` can rebuild the report.

## Evidence status

The checkout used for this package contains the 192-history pilot and the complete 1,280-history main campaign. `make analyse` rebuilds summaries and figures from canonical raw histories under `results/raw/`. The report keeps pilot results separate, reports the main campaign's 320 controls and 960 adversarial histories independently, and uses only the adversarial histories for RQ1 rates and factorial contrasts. The main dataset combines 696 sequential histories with 584 histories produced by two isolated workers; pooled latency is descriptive under those mixed scheduling modes. The initial 449 fault-controller failures are preserved under `results/attempts/`, excluded from analysis, and were rerun with the same ordinals and seeds after the health-check fix. The experiment manifest must remain `COMPLETE` before the package is rebuilt.

## Metadata

Set the team name, member names and student IDs, submission date, and any course-required AI disclosure in `source/submission/metadata.mk` before making the final archive.
