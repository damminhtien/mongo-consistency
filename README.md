# MongoDB consistency experiment

The project contains the MongoDB replica-set runner, fault controller,
client workloads, offline checkers, experiment protocol, and raw-derived
analysis. The scientific source of truth is
[`docs/experimental-protocol.md`](docs/experimental-protocol.md).

The report is authored in [`submission/report.tex`](submission/report.tex).
`make submission` compiles that source to a PDF and packages the PDF with the
MongoDB runtime code. The LaTeX source, analysis scripts, tests, raw histories,
and local build output stay in the repository.

## Development checks

Install the pinned host dependencies:

```text
python3 -m pip install -r requirements-dev.txt
```

Run the local checks:

```text
make test
make check-docs
make check-schemas
make check-runner-isolation
make analyse
make rq3-analyse
make rq4-analyse
make check-release-ready
```

The analysis commands read saved histories and do not connect to MongoDB.
Smoke, pilot, campaign, and fault-coordinator commands require Docker:

```text
make setup
make smoke
make pilot
make experiment
make rq2
make rq3-preflight
make rq3
```

## Code submission

`make submission` creates `output/pdf/mongo-consistency-report.pdf` and
`output/submission/mongo-consistency-submission.zip`. The archive is built
from an explicit runtime allowlist and contains the compiled PDF, package
instructions, and the Compose deployment, runtime configuration, schemas,
runner and offline-analysis scripts, fault controller, and Python package
needed to execute the MongoDB campaigns.

`make check-submission-artifacts` verifies the PDF, manifest, and archive. It
also rejects report source, generated document fragments, tests, raw histories,
and local build output inside the archive.

## Layout

```text
configs/       runtime configurations, schedules, predictions, campaign plans
src/           history model, topology oracle, workloads, checkers, runner code
scripts/       setup and campaign entry points plus offline analysis tools
infra/         runner and fault-controller images
schemas/       versioned runtime record contracts
docs/           protocol, decisions, and analysis notes
results/raw/    canonical campaign histories and manifests
results/summary/ derived JSON and CSV analysis
submission/     authored LaTeX report, references, figures, and package files
tests/          offline tests, kept outside the code submission archive
```

## Evidence boundary

Each trial uses a unique namespace, explicit client session, deterministic seed,
and logged requested and actual routing. A possible write with a lost response
is `INDETERMINATE`. The six outcomes are `PASS`, `VIOLATION`, `UNAVAILABLE`,
`INDETERMINATE`, `PRECONDITION_MISS`, and `HARNESS_ERROR`. Only `PASS` and
`VIOLATION` enter a resolved consistency comparison.
