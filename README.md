# MongoDB consistency experiment

The project contains source, tests, protocol, experiment runner,
offline checker, analysis, and LaTeX package for the DSA5208 MongoDB
client-consistency study. The experiment and its evidence rules are defined in
[docs/experimental-protocol.md](docs/experimental-protocol.md).

## Project documents

- [Assignment summary](docs/assignment.md)
- [Project plan](docs/project-plan.md)
- [Slide alignment](docs/slide-alignment.md)
- [Step 1 decisions](docs/step-1-decisions.md)
- [Experimental protocol](docs/experimental-protocol.md)
- [Report plan](docs/report-plan.md)
- [Work list](TODO.md)

## Commands

~~~bash
make test
make check-docs
make check-schemas
make setup
make smoke
make pilot
make experiment
make rq2
make analyse
make submission
~~~

Install the pinned host-side Python dependencies before running these checks:
`python3 -m pip install -r requirements-dev.txt`. The Docker runner uses the
smaller runtime-only `requirements.txt`.

make setup, make smoke, make pilot, make experiment, and make rq2 require a
running Docker daemon. make analyse rebuilds summaries and plots from saved raw
histories without connecting to MongoDB. make submission builds the report PDF
and checksummed reproduction archive.

make smoke runs 32 histories and is a machinery check, not scientific
evidence. make pilot runs 192 separate histories. RQ1 is a sequential
1,280-history campaign: 320 normal controls and 960 adversarial histories.
RQ2 uses representative configurations under normal operation, secondary
failure, primary failure, and network partition. Campaign sizes and gate
conditions are specified in the protocol.

A first SIGINT or SIGTERM asks the runner to finish the current trial,
clean up faults, and write an INTERRUPTED manifest. Rerunning the same
campaign with resume enabled validates existing histories and continues from
the deterministic plan. A fresh run is allowed only with an empty campaign
directory.

## Layout

~~~text
configs/       settings, schedules, predictions, and campaign definitions
src/           history model, topology oracle, workloads, checkers, analysis
scripts/       setup, campaign, analysis, build, and validation entry points
tests/         offline fixtures and harness checks
schemas/       versioned history and outcome contracts
docs/          project decisions, protocol, slide mapping, report plan
submission/    LaTeX sources, metadata, and package instructions
results/raw/   canonical campaign histories
results/summary/ generated tables and summaries
figures/       generated plots
~~~

## Evidence and safety

Each trial uses a unique namespace, explicit client session, deterministic seed,
and logged requested and actual routing. Possible writes with lost responses
are INDETERMINATE. The six history outcomes are PASS, VIOLATION, UNAVAILABLE,
INDETERMINATE, PRECONDITION_MISS, and HARNESS_ERROR. Only PASS and VIOLATION
enter the consistency denominator.

The runner has no Docker socket, Docker credentials, or unrelated host-data
mount. Predictions and protocol are committed before scientific campaigns.
Generated summaries and report macros are rebuilt from raw histories.
