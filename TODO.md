# Work list

Q1 and Q2 campaign results are complete. Smoke and pilot runs are machinery
checks kept separate from the main campaign histories; the checklist below
tracks remaining project and submission work.

## Protocol and documents

- [x] Record the MongoDB system, three-member Compose baseline, and toolchain targets.
- [x] Preserve the DSA5208 slide-page mapping outside the source PDFs.
- [x] Rewrite the protocol as the scientific source of truth and align the project
  plan, README, report plan, and six-outcome taxonomy.
- [x] Rewrite the report as a reader-oriented LaTeX document; keep the source
  in the repository and package only its compiled PDF with the runtime code.
- [x] Freeze protocol, schedules, schemas, and predictions before the main
  campaign; the RQ1 manifest records the committed input revisions and hashes.

## Data model and checkers

- [x] Store preconditions as first-class trial data.
- [x] Define the six history outcomes; exclude precondition misses from database metrics.
- [x] Use one logical document and explicit application dependencies for MW/WFR.
- [x] Add offline RYW, MR, MW, and WFR checkers and same-key fixtures.
- [x] Validate every serialized history against the committed JSON Schemas.
- [x] Add fixtures for invalid direct roles, missing setup records, and every
  malformed or ambiguous history boundary.

## Topology and workloads

- [x] Add direct-member TopologyOracle; require one primary and two secondaries.
- [x] Add RYW and MR schedules with stale-state checks before subject reads.
- [x] Add MW and WFR schedules with explicit session dependencies and fault cleanup.
- [x] Record requested route, command-monitor server address, driver-reported
  role, and direct role observation separately.
- [x] Require independent post-heal observations across all three members in
  MW/WFR checkers.
- [x] Verify direct routing and post-heal convergence on the live Compose set.
- [x] Prove client access survives replication-path isolation for each targeted member.
- [x] Exercise elections, healing, and controller cleanup against live services.

## Campaign runner and provenance

- [x] Generate a deterministic 32-history smoke plan and fail its gate on
  incomplete coverage, harness errors, or more than 5% precondition misses.
- [x] Add graceful shutdown and atomic resumable manifests.
- [x] Add distinct prediction/protocol/runner provenance fields and refuse
  non-smoke runs without frozen clean provenance.
- [x] Verify actual Python, PyMongo, MongoDB server, Docker, Compose versions and
  image digest with a live setup.
- [x] Test resume rejection for changed provenance and altered history hashes.
- [x] Confirm the runner container cannot access Docker socket, credentials, or
  unrelated host files; save the check in
  `results/validation/runner-isolation.json`.

## Validation and campaigns

- [x] Run `make test`, `make check-docs`, `make check-schemas`, and `make analyse`
  after the report/package rewrite.
- [x] Start Docker Desktop and run make setup for the live campaigns.
- [x] Run and inspect the smoke machinery diagnostic. Its dirty-runner failures
  are separate from the clean, frozen main campaign and are not RQ1 outcomes.
- [x] Freeze predictions and protocol before RQ1; the campaign manifest records
  prediction commit `b79b567` and protocol commit `c7e3cf9`.
- [ ] Run the 128-history pilot; require zero harness errors and precondition
  misses at or below 5% per property.
- [ ] Run reduced sequential RQ1: 96 normal controls plus 160 adversarial
  histories, 256 in total.
- [x] Implement the grouped RQ2 runner and host-only fault coordinator for
  C1/C3/C4/C6, F1/F2/F3, and RYW/MR/MW/WFR, with a 432-history plan across 36
  episodes and unique keys/sessions per history.
- [x] Freeze the clean protocol/runner provenance, then run the 384-history
  core and extend four partition signature cells to 20 repetitions (432 total);
  use RQ1 normal histories as the separate baseline.
- [x] Validate manifests, hashes, routing, cleanup, and completeness before analysis.

## Analysis and code submission

- [x] Rebuild every summary and figure from raw histories without MongoDB.
- [x] Publish generated RQ2 outcome, availability, latency, rollback, election,
  and recovery results in the report and experiment notes.
- [x] Add generated-result drift checks to CI.
- [x] Derive aggregate outcome counts and metrics as machine-readable JSON and
  CSV from canonical histories.
- [x] Build and inspect the checksummed PDF and runtime-code archive from a
  clean CI checkout.
- [x] Commit coherent slices, inspect staged diffs, run git diff --check, and
  push main.

Q1 / Task 1 status: pending reduced rerun. The active plan is 256 histories:
96 normal controls and 160 adversarial histories. Counts must be regenerated
from the raw histories after the run; the superseded 1,280-history result is
not evidence for the reduced campaign.

Q2 / Task 2 status: complete. Execution evidence (20 September 2026):
`make rq2` completed 432/432
histories and 36/36 episodes with verified fault application and converged
recovery. Record/schema validation passed and `make analyse` rebuilt the summary
and figures. The full outcome table and signature-cell observations are in
[docs/rq2-results.md](docs/rq2-results.md). The compiled PDF and runtime-code
archive are checked by `make check-submission-artifacts`; report source and
tests remain outside the archive.
