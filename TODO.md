# Work list

The experiment is not complete until live smoke, the frozen pilot, RQ1, RQ2,
offline analysis, and the rendered submission all pass the evidence checks.

## Protocol and documents

- [x] Record the MongoDB system, three-member Compose baseline, and toolchain targets.
- [x] Preserve the DSA5208 slide-page mapping outside the source PDFs.
- [x] Rewrite the protocol as the scientific source of truth and align the project
  plan, README, report plan, and six-outcome taxonomy.
- [ ] Rewrite all LaTeX sections and the package README to remove stale
  parallel-campaign narratives and hard-coded historical measurements.
- [ ] Freeze protocol, schedules, schemas, and predictions in a clean commit
  after the live smoke passes.

## Data model and checkers

- [x] Store preconditions as first-class trial data.
- [x] Define the six history outcomes; exclude precondition misses from database metrics.
- [x] Use one logical document and explicit application dependencies for MW/WFR.
- [x] Add offline RYW, MR, MW, and WFR checkers and same-key fixtures.
- [ ] Validate every serialized history against the committed JSON Schemas.
- [ ] Add fixtures for invalid direct roles, missing setup records, and every
  malformed or ambiguous history boundary.

## Topology and workloads

- [x] Add direct-member TopologyOracle; require one primary and two secondaries.
- [x] Add RYW and MR schedules with stale-state checks before subject reads.
- [x] Add MW and WFR schedules with explicit session dependencies and fault cleanup.
- [x] Record requested route, command-monitor server address, driver-reported
  role, and direct role observation separately.
- [x] Require independent post-heal observations across all three members in
  MW/WFR checkers.
- [ ] Verify direct routing and post-heal convergence on the live Compose set.
- [ ] Prove client access survives replication-path isolation for each targeted member.
- [ ] Exercise elections, healing, and controller cleanup against live services.

## Campaign runner and provenance

- [x] Generate a deterministic 32-history smoke plan and fail its gate on
  incomplete coverage, harness errors, or more than 5% precondition misses.
- [x] Add graceful shutdown and atomic resumable manifests.
- [x] Add distinct prediction/protocol/runner provenance fields and refuse
  non-smoke runs without frozen clean provenance.
- [ ] Verify actual Python, PyMongo, MongoDB server, Docker, Compose versions and
  image digest with a live setup.
- [ ] Test resume rejection for changed provenance and altered history hashes.
- [ ] Confirm the runner container cannot access Docker socket, credentials, or
  unrelated host files.

## Validation and campaigns

- [ ] Run all offline gates after the LaTeX/package rewrite.
- [ ] Start Docker Desktop and run make setup.
- [ ] Run make smoke; inspect every property and require a passing gate.
- [ ] Freeze predictions and protocol after smoke passes.
- [ ] Run the 192-history pilot; require zero harness errors and precondition
  misses at or below 5% per property.
- [ ] Run sequential RQ1: 320 normal controls plus 960 adversarial histories.
- [x] Implement the grouped RQ2 runner and host-only fault coordinator for
  C1/C3/C4/C6, F1/F2/F3, and RYW/MR/MW/WFR, with a 432-history plan across 36
  episodes and unique keys/sessions per history.
- [x] Freeze the clean protocol/runner provenance, then run the 384-history
  core and extend four partition signature cells to 20 repetitions (432 total);
  use RQ1 normal histories as the separate baseline.
- [x] Validate manifests, hashes, routing, cleanup, and completeness before analysis.

## Analysis and submission

- [x] Rebuild every summary and figure from raw histories without MongoDB.
- [ ] Add generated-result drift checks to CI.
- [ ] Derive every outcome and metric in LaTeX from generated macros.
- [ ] Render and visually inspect the final PDF after RQ1/RQ2 evidence is complete.
- [ ] Build and inspect the checksummed archive from a clean clone.
- [ ] Commit coherent slices, inspect staged diffs, run git diff --check, and
  push main.

RQ2 execution evidence (20 September 2026): `make rq2` completed 432/432
histories and 36/36 episodes with verified fault application and converged
recovery. Record/schema validation passed and `make analyse` rebuilt the summary
and figures. `make check-release-ready` remains blocked by two pending team
student IDs; it reported no RQ2 completeness or analysis errors. The full outcome
table and signature-cell observations are in [docs/rq2-results.md](docs/rq2-results.md).
