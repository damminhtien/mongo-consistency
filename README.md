# MongoDB consistency experiment

The project records a reproducible DSA5208 experiment for MongoDB client-visible consistency. The implementation records raw operation histories, checks RYW, MR, MW, and WFR offline, and builds the submission package from those records.

The cluster runner uses a three-member MongoDB 7.0.34 replica set in Docker Compose with Python 3.14.7, PyMongo 4.18.1, Docker Engine 29.8.0, and Compose 5.5.1. The checkout contains the 192-history pilot and the complete 1,280-history main campaign. Analysis and conclusions remain limited to the recorded versions, settings, workloads, and fault schedules.

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
make check-schemas
make setup
make pilot
make experiment
make experiment-parallel
make experiment-fresh
make analyse
make submission
```

`make setup` checks the pinned toolchain, starts and verifies the local replica set, and records the MongoDB image digest. `make check-schemas` validates the record contracts and any generated records. `make pilot`, `make experiment`, and `make experiment-parallel` require Docker. `make analyse` consumes raw histories and does not require a live MongoDB connection. `make submission` builds the PDF and reproduction archive.

`make experiment` is resumable. The first Ctrl-C or termination request finishes the active trial and writes an `INTERRUPTED` manifest; running the command again validates and skips completed histories. Use `make experiment-fresh` only for an empty campaign directory.

`make experiment-parallel` uses two workers by default. Each worker gets its own Compose project, three-member replica set, client and replica networks, named volumes, host ports, and result directory. The coordinator stops the baseline stack before starting workers, forwards shutdown signals, and merges only histories whose trial identity and bytes pass validation. Set `PARALLEL_WORKERS=3` or another value up to 32 when the machine has enough resources. Worker scratch data stays under `results/parallel/` and is ignored; `make analyse` reads the merged files under `results/raw/`. A campaign is complete only when the canonical manifest says `COMPLETE` and lists all planned histories.

The main campaign has 320 normal-control histories and 960 adversarial histories, for 1,280 histories total. The 192-history pilot remains separate; RQ1 rates and factorial contrasts use only main-campaign adversarial histories. The pilot checks timing and topology preconditions and is not pooled into the main estimates.

## Layout

```text
configs/       frozen configurations, schedules, predictions, and campaign rules
src/           history model, checkers, runner, routing, faults, and analysis
scripts/       command entry points and validation tools
tests/         offline fixtures and integration checks
results/raw/   canonical trial histories and manifests
results/parallel/ worker scratch histories for isolated workers
results/summary/ derived tables and metrics
figures/       generated report figures
schemas/       versioned record contracts
submission/    LaTeX report and package metadata
```

## Safety and reproducibility

The runner has no Docker socket, Docker credentials, host filesystem mount, or unrelated host-data access. Reads and writes use `retryReads=false` and `retryWrites=false`. A possible mutation after a timeout is recorded as `INDETERMINATE`. `HARNESS_ERROR` and `UNSUPPORTED` remain outside database metrics.

Every trial has a unique namespace, a seeded campaign ordinal, actual routing details, software versions, a fault event log, and a canonical history hash. The prediction manifest is committed before result files. Generated files remain outside the source inputs until the analysis command writes them.
