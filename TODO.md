# Execution TODO

This is the implementation checklist for the MongoDB client-centric consistency project. Work from top to bottom unless a task is explicitly marked optional. Keep this file checked against the actual repository state; do not check an item because a command merely exists.

Source: [DSA5208 Scalable Distributed GRP Project](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), including the [project plan](docs/project-plan.md), [experimental protocol](docs/experimental-protocol.md), and [report plan](docs/report-plan.md).

Deadline stated by the source: submit the report, code, scripts, and reproduction instructions through Canvas no later than **27 September 2026**.

## Definition of done

- [ ] A fresh checkout can set up a reproducible three-member MongoDB replica set.
- [ ] The selected configurations and pre-registered predictions are recorded before result analysis.
- [ ] Adversarial workloads target RYW, MR, MW, and WFR under normal operation, node failure, and network partition.
- [ ] The independent checker evaluates logical versions and explicit dependencies, not wall-clock timestamps.
- [ ] `PASS`, `VIOLATION`, and `UNAVAILABLE` remain separate in raw records, summaries, and the report.
- [ ] Raw histories can be replayed offline to regenerate summaries and figures deterministically.
- [ ] The PDF report, source code, scripts, tests, citations, and AI-use disclosure are complete and reproducible.

## Completed groundwork

- [x] Normalize the assignment brief into [docs/assignment.md](docs/assignment.md).
- [x] Record the MongoDB/PyMongo/Docker Compose decision in [docs/project-plan.md](docs/project-plan.md).
- [x] Record the history schema, predicates, fault schedules, and outcome classes in [docs/experimental-protocol.md](docs/experimental-protocol.md).
- [x] Record the proposed paper structure, figures, tables, and evidence rules in [docs/report-plan.md](docs/report-plan.md).

## 0. Freeze scope and decisions

- [ ] Confirm the group roster has no more than three people; do not put unverified names into the report.
- [ ] Confirm MongoDB replica set is the final technology choice; record any change and its rationale.
- [ ] Freeze the baseline as three data-bearing MongoDB members in Docker Compose.
- [ ] Write down the exact MongoDB server version, PyMongo version, Python version, Docker Engine version, and Compose version to be used.
- [ ] Decide how logical versions are represented and compared; document the ordering rule before implementing workloads.
- [ ] Decide the trial repetition count, timeout policy, workload seed policy, and clean-state policy before the pilot.
- [ ] Decide which configurations are in the main campaign. Do not run the full Cartesian product without a distinct prediction for each selected cell.
- [ ] Record out-of-scope items: Kubernetes, Docker Swarm, Prometheus/Grafana, service meshes, and advanced network impairments that do not answer a specific question.
- [ ] Start a decision log in the repository or commit messages for changes to versions, configuration selection, predicates, or fault topology.

### Gate 0 — scope is reproducible

- [ ] A reviewer can identify the exact stack, baseline topology, selected configurations, repetitions, and planned fault scenarios without guessing.
- [ ] No result-dependent decision remains hidden in code or in the analysis notebook/script.

## 1. Bootstrap the repository

- [ ] Create the implementation layout:
  - [ ] `src/` for the runner, recorder, checker, and analysis modules.
  - [ ] `scripts/` for setup, teardown, replica-set initialization, and fault injection.
  - [ ] `tests/` for checker, schema, and harness tests.
  - [ ] `configs/` for versioned experiment and fault schedules.
  - [ ] `results/raw/` for canonical operation histories.
  - [ ] `results/summary/` for derived metrics and tables.
  - [ ] `figures/` for generated plots.
- [ ] Add a pinned Python dependency manifest and a documented environment creation command.
- [ ] Add a `Makefile` with `setup`, `experiment`, `analyse`, `test`, and cleanup targets.
- [ ] Add `.gitignore` rules for virtual environments, temporary cluster state, logs, caches, and generated files that should not be tracked.
- [ ] Define which raw traces and figures are committed as report evidence and which are generated artifacts; include at least small fixtures for offline tests.
- [ ] Add a repository-level configuration/schema version to every machine-readable artifact.
- [ ] Ensure no credentials, tokens, personal browser data, or unrelated host data can be read by the runner or uploaded as an artifact.

### Gate 1 — command surface exists

- [ ] `make test` runs without a live MongoDB cluster for checker fixtures.
- [ ] `make setup`, `make experiment`, and `make analyse` fail with a clear message when their prerequisites are missing.
- [ ] A clean checkout contains enough instructions to discover the intended execution order.

## 2. Build and validate the Docker Compose harness

- [ ] Define three uniquely named data-bearing MongoDB services with persistent-but-disposable test volumes.
- [ ] Configure one replica-set name and stable service-to-service addresses.
- [ ] Add health checks and readiness polling; do not assume container start means replica-set readiness.
- [ ] Implement idempotent replica-set initialization and verify the expected member count and roles.
- [ ] Make `make setup` wait for one `PRIMARY` and the expected `SECONDARY` members.
- [ ] Implement clean teardown/reset so each trial can start from a known state.
- [ ] Add a health/status command that records member IDs, roles, states, and server versions.
- [ ] Test a normal read/write round trip through PyMongo using the pinned driver version.
- [ ] Test stopping and restarting one secondary without corrupting the harness.
- [ ] Test stopping the current primary, waiting for election, and reconnecting after the new primary is available.
- [ ] Verify the runner can target the intended member when the experiment requires a direct stale-member read.
- [ ] Record any limitation where the client path and replication path cannot be separated.

### Gate 2 — three-node baseline is stable

- [ ] A fresh `make setup` reaches the expected topology repeatedly.
- [ ] A reset removes prior trial state and does not depend on manual Docker Desktop actions.
- [ ] Secondary failure and primary election are observable through structured events, not only terminal output.
- [ ] The harness can be stopped and recreated without changing the documented configuration.

## 3. Define the data model and logical history contract

- [ ] Choose a minimal document/key model that can express ordered writes, observed versions, and dependent writes.
- [ ] Define the logical version domain and total/partial ordering used by each checker.
- [ ] Ensure every write and read record contains the key and the logical version/value observed or produced.
- [ ] Encode client program order explicitly, for example with a per-client sequence number.
- [ ] Encode causal dependencies explicitly; for WFR, record a field such as `y.source_version = v_r`.
- [ ] Do not use wall-clock timestamps, MongoDB object-ID ordering, or response time as a substitute for logical version ordering.
- [ ] Define how visibility is observed for MW before writing the MW checker tests.
- [ ] Define how a history is considered complete, truncated, malformed, or invalid.
- [ ] Define canonical serialization and hashing for a history so the same input yields the same analysis result.

## 4. Implement the independent history recorder

- [ ] Emit one structured record per attempted operation.
- [ ] Include the required fields:
  - [ ] `trial_id`, `client_id`, `session_id`, and `operation_id`.
  - [ ] `operation_type`, `key`, and `value_or_version`.
  - [ ] `target_node`, read concern, write concern, and causal-session flag.
  - [ ] Invocation/response interval, success/error, and dependency metadata.
- [ ] Add client sequence, fault event ID, member ID/role, error code, timeout category, and runner/container identifiers where available.
- [ ] Flush records safely during faults so a process failure does not silently erase the history prefix.
- [ ] Record fault start/end events in the same trial namespace as operations.
- [ ] Record the configuration manifest and software versions beside every raw history.
- [ ] Validate records on write and again during offline replay.
- [ ] Add a small fixture history for each required consistency property and each outcome class.

## 5. Implement and test the formal checkers first

- [ ] Implement a strict parser/validator that rejects or explicitly diagnoses missing fields, duplicate operation IDs, impossible dependencies, and unknown schema versions.
- [ ] Implement RYW: after `W_c(x, v_w)` and a later `R_c(x, v_r)`, flag a counterexample when `v_r < v_w`.
- [ ] Implement MR: for successive reads `R_i(x, v_i)` and `R_(i+1)(x, v_j)` by one client, flag a counterexample when `v_j < v_i`.
- [ ] Implement MW using the previously documented visibility observation for ordered writes `W1 -> W2`.
- [ ] Implement WFR using explicit read-to-dependent-write metadata and the documented visibility rule.
- [ ] Return structured evidence for every finding: trial, operations, versions, dependency, member, configuration, and fault event.
- [ ] Classify outcomes exactly as `PASS`, `VIOLATION`, or `UNAVAILABLE`.
- [ ] Ensure errors, blocks, and timeouts before a successful history are never counted as consistency violations.
- [ ] Add unit tests for a valid history and a minimal counterexample for RYW, MR, MW, and WFR.
- [ ] Add tests proving unavailable operations are excluded from the violation denominator.
- [ ] Add tests for malformed records and verify fail-closed diagnostics.
- [ ] Add deterministic replay tests: identical canonical history plus checker version produces identical output.

### Gate 3 — checker contract is trusted

- [ ] All checker tests pass without MongoDB.
- [ ] Each predicate is executable from a fixture and produces an inspectable counterexample.
- [ ] A reviewer can explain every denominator and every outcome class from the checker output.
- [ ] The checker has no dependency on workload-generator implementation details.

## 6. Build adversarial workloads

- [ ] Implement one minimal deterministic workload/state machine per property before adding random or high-throughput workloads.
- [ ] RYW workload: write a version, then read the same key through the selected client/session path.
- [ ] MR workload: read an initial version, then force or target a later read that could return an older replica state.
- [ ] MW workload: issue two ordered writes and observe whether the second can become visible without the required predecessor.
- [ ] WFR workload: read a version, attach it as explicit dependency metadata to a dependent write, and test visibility of the dependent result.
- [ ] Add valid control histories for each workload so a detected violation is not caused by an invalid workload itself.
- [ ] Make client IDs, session IDs, operation IDs, and logical sequences deterministic for a fixed seed.
- [ ] Keep workload generation separate from checking and analysis.
- [ ] Define per-workload timeouts and a bounded stop condition for blocked operations.
- [ ] Store the exact workload parameters and seed in the trial manifest.

## 7. Pre-register predictions and select the campaign

- [ ] Create the prediction matrix before running the main campaign.
- [ ] For each selected configuration × fault condition × property, record expected `PASS`, possible `VIOLATION`, or expected `UNAVAILABLE` behaviour and the theory/rationale.
- [ ] Cover the candidate configurations:
  - [ ] C1: `local` / `w: 1` / causal off.
  - [ ] C2: `local` / `w: 1` / causal on.
  - [ ] C3: `majority` / `w: 1` / causal on.
  - [ ] C4: `local` / `majority` / causal on.
  - [ ] C5: `majority` / `majority` / causal off.
  - [ ] C6: `majority` / `majority` / causal on.
- [ ] If a configuration is removed from the main campaign, document why its prediction is redundant or not testable.
- [ ] Confirm the matrix was committed/versioned before result analysis begins.
- [ ] Define the exact normal, secondary-failure, primary-failure/election, and partition schedules to be run for each selected configuration.
- [ ] Define the repetition count and any fixed/randomized schedule order.

### Gate 4 — no post-hoc predictions

- [ ] The prediction matrix has a commit timestamp/SHA or other repository evidence predating the main result artifacts.
- [ ] The analysis code reads predictions as input and cannot silently rewrite them from observations.

## 8. Implement controlled fault injection

- [ ] Normal operation: establish baseline availability and latency.
- [ ] Secondary failure: stop one secondary, record the fault event, run the selected workload, and restore the member.
- [ ] Primary failure: identify the current primary, stop it, record election and temporary unavailability, wait for recovery, and run post-election checks.
- [ ] Network partition: keep the isolated member alive and block the explicitly documented client/replication path.
- [ ] Verify that a partition test can actually query a stale member when that is part of the question; otherwise mark the test unsupported and redesign the topology.
- [ ] Record exact fault start/end times for operational diagnostics while keeping logical predicates independent of those times.
- [ ] Add cleanup and recovery checks after every fault so one trial cannot contaminate the next.
- [ ] Add latency, packet loss, or bandwidth restrictions only after writing the question they answer and the expected effect.
- [ ] Do not treat a killed node as equivalent to a network partition.

## 9. Run the pilot and quality gates

- [ ] Run checker fixtures and confirm all four predicates before connecting to MongoDB.
- [ ] Run one end-to-end trial for each selected configuration under normal operation.
- [ ] Run one bounded secondary-failure and primary-election smoke trial.
- [ ] Run a partition smoke trial and inspect whether the intended stale-state path is real.
- [ ] Verify every raw record contains the required fields and links to the trial manifest.
- [ ] Manually inspect at least one trace for each property, including one unavailable case.
- [ ] Confirm the analysis result can be regenerated after deleting all derived summaries and figures.
- [ ] Confirm the checker does not count timeouts/errors as violations.
- [ ] Fix schema, topology, or instrumentation problems now; do not patch them silently during the full campaign.
- [ ] Freeze the pilot-approved versions, configurations, workload parameters, and fault schedules.

### Gate 5 — pilot is publishable evidence

- [ ] The pilot produces a complete raw history, checker output, manifest, and reproducible summary.
- [ ] No required field, fault event, member identity, or outcome category is missing.
- [ ] The partition topology is either validated or explicitly recorded as unavailable for the intended stale-read test.

## 10. Run the main campaign

- [ ] Create a unique trial ID for every configuration × fault × property × repetition.
- [ ] Reset to known logical state before every independent trial.
- [ ] Record the exact code/configuration/checker versions used by every trial.
- [ ] Run the pre-registered normal-operation scenarios.
- [ ] Run the pre-registered secondary-failure scenarios.
- [ ] Run the pre-registered primary-failure/election scenarios.
- [ ] Run the pre-registered network-partition scenarios.
- [ ] Preserve raw histories even when a run fails or becomes unavailable.
- [ ] Record aborted, timed-out, and infrastructure-failure trials separately from valid unavailable operations.
- [ ] Monitor disk space and output integrity without changing the workload or dropping traces.
- [ ] Repeat failed trials only under a documented retry rule; never overwrite the original failed record.
- [ ] Do not add configurations or fault types because their preliminary results look interesting unless the change is recorded as a new campaign.

### Optional 5-member extension

- [ ] Only consider five data-bearing members after the three-node campaign passes all gates.
- [ ] Re-run setup, election, failure, partition, and checker smoke tests before collecting comparison results.
- [ ] Keep five-member results clearly separated from the baseline and state why the extension answers a question.

## 11. Analyse results offline

- [ ] Make `make analyse` consume raw histories and manifests, not a live MongoDB connection.
- [ ] Validate and canonicalize every input history before aggregation.
- [ ] Compute violation rate as `violating successful histories / successful histories`.
- [ ] Compute availability rate as `successful operations / attempted operations`.
- [ ] Keep `PASS`, `VIOLATION`, and `UNAVAILABLE` counts visible for every cell.
- [ ] Report p50, p95, and p99 latency; do not report only the mean.
- [ ] Measure primary-failure/election recovery time where the event is meaningful.
- [ ] Generate the configuration × failure × property result matrix/heatmap.
- [ ] Generate at least one exact-version counterexample trace for every reported real violation.
- [ ] Generate a prediction-versus-observation-versus-explanation table.
- [ ] Record repetition counts, workload shape, fault schedule, and software versions behind every aggregate.
- [ ] Check for contradictory or impossible records and fail the analysis with an actionable diagnostic.
- [ ] Confirm that zero observed violations is worded as bounded evidence, never as proof of a universal guarantee.
- [ ] Review results separately for normal operation, secondary failure, election, and partition; do not collapse all failures into one category.

### Gate 6 — analysis is auditable

- [ ] A reviewer can trace every reported number to raw histories and a checker version.
- [ ] Re-running analysis from the same raw inputs produces byte-stable or explicitly versioned equivalent outputs.
- [ ] No plot or table contains a cell whose denominator or outcome classification is ambiguous.

## 12. Write and verify the report

- [ ] Write the abstract only after the analysis is frozen.
- [ ] Describe the research questions and contribution.
- [ ] Describe RYW, MR, MW, WFR, logical versions, causal precedence, and the relevant MongoDB semantics.
- [ ] Describe the deployment architecture, exact versions, installation, reset, and fault controls.
- [ ] Include the pre-measurement prediction matrix and explain why each configuration was selected.
- [ ] Describe the workload generators, history schema, formal predicates, controls, repetitions, and fault model.
- [ ] Present results with PASS/VIOLATION/UNAVAILABLE separation, availability, latency, and recovery metrics.
- [ ] Explain representative histories and the mechanisms behind observed behaviour.
- [ ] State all threats to validity: one-host logical replicas, synthetic Docker networking, nondeterministic scheduling/replication, finite trials, minimal workloads, and exact-version scope.
- [ ] Describe the three-command reproduction workflow: `make setup`, `make experiment`, `make analyse`.
- [ ] Add the conclusion with bounded claims and unanswered questions.
- [ ] Add references for course lectures, MongoDB, PyMongo, fault-injection tooling, and AI usage.
- [ ] Add appendices with Compose/setup commands, fault scripts, selected raw traces, checker pseudocode, and extra plots.
- [ ] Include the priority figures: architecture, four minimal counterexamples, fault topology, result heatmap, representative trace, consistency/availability trade-off, and latency distribution.
- [ ] Include the priority tables: predicates, versions, predictions, controls/variables, quantitative results, prediction/observation/explanation, and limitations/mitigations.
- [ ] Remove installation screenshots unless one directly proves an experimental observation.

## 13. Reproducibility and submission closeout

- [ ] Test from a fresh checkout on the documented host/environment.
- [ ] Run the complete command sequence without undocumented manual edits.
- [ ] Verify Compose teardown/reset leaves no hidden state that changes results.
- [ ] Verify all source code, scripts, tests, and brief reproduction instructions are present.
- [ ] Verify report references point to the exact versions and tools actually used.
- [ ] Verify raw traces used in the report are present, readable, and linked to summaries/figures.
- [ ] Verify the final PDF opens, has readable figures/tables, and contains no placeholder text.
- [ ] Verify the AI-use disclosure is complete and accurate.
- [ ] Record the final repository commit SHA and the commands used for the final artifact build.
- [ ] Submit the PDF and reproducibility package to Canvas before 27 September 2026.
- [ ] Archive the final raw-data manifest, summary manifest, figure manifest, and report checksum if permitted by the course submission rules.

## 14. Parking lot — do only with a written question

- [ ] Five-member replica set after the three-node baseline is stable.
- [ ] Controlled latency injection.
- [ ] Controlled packet loss.
- [ ] Bandwidth restriction.
- [ ] Any orchestration or observability stack beyond Docker Compose and the experiment scripts.

Do not check a parking-lot item merely to make the system look more sophisticated. Each extension must have a research question, prediction, fault schedule, and interpretation plan.
