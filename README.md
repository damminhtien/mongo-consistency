# MongoDB consistency experiment

The project records a reproducible DSA5208 experiment for MongoDB client-visible consistency. The implementation records raw operation histories, checks RYW, MR, MW, and WFR offline, and builds the submission package from those records.

The cluster runner is being built against a three-member MongoDB replica set in Docker Compose. Until a campaign produces raw histories, the repository makes no result claim.

## Canonical documents

- [Assignment summary](docs/assignment.md)
- [Project plan](docs/project-plan.md)
- [Slide alignment](docs/slide-alignment.md)
- [Step 1 decisions](docs/step-1-decisions.md)
- [Experimental protocol](docs/experimental-protocol.md)
- [Report plan](docs/report-plan.md)
- [Work list](TODO.md)

## Commands

```bash
make test
make check-docs
make setup
make pilot
make experiment
make analyse
make submission
```

`make setup` checks the pinned toolchain, starts and verifies the local replica set, and records the MongoDB image digest. `make pilot` and `make experiment` require Docker. `make analyse` consumes raw histories and does not require a live MongoDB connection. `make submission` builds the PDF and reproduction archive.

The main campaign has 320 normal-control histories and 960 adversarial histories, for 1,280 histories total. The pilot is smaller and is used to validate timing and topology preconditions before the main campaign.

## Layout

```text
configs/       frozen configurations, schedules, predictions, and campaign rules
src/           history model, checkers, runner, routing, faults, and analysis
scripts/       command entry points and validation tools
tests/         offline fixtures and integration checks
results/raw/   canonical trial histories and manifests
results/summary derived tables and metrics
figures/       generated report figures
submission/    LaTeX report and package metadata
```

## Safety and reproducibility

The runner has no Docker socket, Docker credentials, host filesystem mount, or unrelated host-data access. Reads and writes use `retryReads=false` and `retryWrites=false`. A possible mutation after a timeout is recorded as `INDETERMINATE`. `HARNESS_ERROR` and `UNSUPPORTED` remain outside database metrics.

Every trial has a unique namespace, a seeded campaign ordinal, actual routing details, software versions, a fault event log, and a canonical history hash. The prediction manifest is committed before result files. Agentic metadata such as `.codex/` and `AGENTS*` is ignored and is not part of the submission repository.
