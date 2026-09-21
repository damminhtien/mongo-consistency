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
# Requires a clean checkout with frozen provenance and a running Docker daemon.
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
The complete campaign is stored under the single canonical
`results/raw/experiment/` directory.
RQ2 is registered for C1/C3/C4/C6, three fault conditions, and all four
properties. Its 384-history core is extended by 48 histories in four partition
signature cells, for 432 histories total. RQ2 reuses the RQ1 normal histories
as its descriptive baseline and runs no additional normal condition.

The RQ2 design groups trials into 24 core fault episodes and 12 partition
extension episodes. Histories use unique keys and sessions; election and
recovery timing is summarized once per episode. The grouped runner and
`make rq2` campaign completed on 20 September 2026: 432 histories across 36
episodes, all with verified fault application and converged recovery. Outcomes
were 352 PASS, 40 VIOLATION, and 40 INDETERMINATE; the latter are not counted as
consistency passes or violations. The C1 partition RYW and MW violations were
reproduced in all 20 signature repetitions. See [RQ2 results](docs/rq2-results.md).
The host coordinator controls faults through a temporary, narrowly mounted IPC
directory; the runner receives no Docker socket.

## Release package

CI rebuilds analysis from committed raw histories before producing its PDF, ZIP
archive, and checksum manifest. A `v*` tag publishes those three files as
GitHub Release assets. The release workflow requires complete RQ1 and RQ2
campaigns and all team student IDs before creating a release.

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
