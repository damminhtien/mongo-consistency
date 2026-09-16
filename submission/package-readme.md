# MongoDB consistency submission package

The package contains the report PDF, the LaTeX source, the repository documents, and the scripts and tests available in the checkout used for the build.

## Rebuild

From a checkout with a LaTeX engine installed:

```bash
make submission
```

The command writes `output/pdf/mongo-consistency-report.pdf`, `output/submission/mongo-consistency-submission.zip`, and a SHA-256 manifest. The archive source is under `source/` so that `make -C source submission` can rebuild the report.

## Evidence status

The current checkout has no MongoDB run, raw operation history, summary, or figure. The report labels those fields `NO_DATA`; it does not fill them with invented measurements. Add the recorded artifacts under `results/` and `figures/`, then run `make check-schemas` and the same command again.

## Metadata

Set the team name, member names and student IDs, submission date, and any course-required AI disclosure in `source/submission/metadata.mk` before making the final archive.
