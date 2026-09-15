# Execution checklist

Use this list in order. Mark an item only after it has been checked in the repository or in a recorded trial.

Source: [DSA5208 project brief and plan](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), read on 15 September 2026.

The source gives 27 September 2026 as the Canvas deadline.

## Finish conditions

- [ ] A fresh checkout can start a three-member MongoDB replica set.
- [ ] The chosen settings and predictions are recorded before the main results are analysed.
- [ ] Workloads cover RYW, MR, MW, and WFR during normal operation, node failure, and network partition.
- [ ] The checker uses logical versions and recorded dependencies, not wall-clock time.
- [ ] `PASS`, `VIOLATION`, and `UNAVAILABLE` stay separate in records, tables, and prose.
- [ ] Raw histories can be replayed offline to rebuild tables and figures.
- [ ] The PDF, code, scripts, tests, citations, and reproduction steps are ready for submission.

## Already written

- [x] Put the course requirements in [docs/assignment.md](docs/assignment.md).
- [x] Put the system choice, research questions, settings, and limits in [docs/project-plan.md](docs/project-plan.md).
- [x] Put the history format, checker rules, fault cases, and metrics in [docs/experimental-protocol.md](docs/experimental-protocol.md).
- [x] Put the report structure and output list in [docs/report-plan.md](docs/report-plan.md).
- [x] Add repository documentation checks for Markdown, text, LaTeX, and PDF files.
- [x] Add a LaTeX report layout and `make submission` package build.

## 1. Fix the choices

- [ ] Confirm the group has no more than three people.
- [ ] Confirm MongoDB is the final database choice. Record a change and its reason if the choice changes.
- [ ] Fix the baseline at three data-bearing members in Docker Compose.
- [ ] Record the MongoDB server, PyMongo, Python, Docker Engine, and Compose versions.
- [ ] Choose the representation and ordering rule for logical versions.
- [ ] Choose the trial count, timeout policy, seed policy, and clean-state policy.
- [ ] Choose the settings for the main campaign and explain why each one has a different prediction.
- [ ] Keep Kubernetes, Docker Swarm, Prometheus, Grafana, service meshes, and unnecessary network impairments out of the baseline.
- [ ] Record changes to versions, settings, predicates, or topology in the repository history.

### Scope check

- [ ] A reviewer can find the stack, topology, settings, trial count, and fault cases without asking for missing details.
- [ ] No decision depends on a result that has not been collected yet.

## 2. Set up the repository

- [ ] Create `src/` for the runner, recorder, checker, and analysis code.
- [ ] Create `scripts/` for setup, teardown, replica-set initialization, and fault injection.
- [ ] Create `tests/` for checker, schema, and harness tests.
- [ ] Create `configs/` for settings, workloads, repetitions, and fault schedules.
- [ ] Create `results/raw/` for operation histories.
- [ ] Create `results/summary/` for derived metrics and tables.
- [ ] Create `figures/` for generated plots.
- [ ] Add a pinned Python dependency file and a documented environment setup command.
- [ ] Add a `Makefile` with `setup`, `experiment`, `analyse`, `test`, and cleanup targets.
- [ ] Ignore virtual environments, Docker state, logs, caches, and generated output that should not be committed.
- [ ] Decide which raw histories and figures belong in Git. Keep small fixtures for offline tests.
- [ ] Add a schema version to each machine-readable result file.
- [ ] Check that the runner cannot read credentials, browser data, or unrelated host files.

### Command check

- [ ] `make test` works without a running MongoDB cluster.
- [ ] Each of `make setup`, `make experiment`, and `make analyse` reports missing prerequisites clearly.
- [ ] The repository documents the command order from a clean checkout.

## 3. Build the Docker Compose harness

- [ ] Define three named MongoDB services with disposable test volumes.
- [ ] Set one replica-set name and stable service addresses.
- [ ] Add health checks and readiness polling.
- [ ] Make replica-set initialization idempotent.
- [ ] Make `make setup` wait for one `PRIMARY` and the expected `SECONDARY` members.
- [ ] Add a reset command that removes trial data and returns to a known state.
- [ ] Add a status command that records member IDs, roles, states, and server versions.
- [ ] Run a PyMongo read/write round trip with the pinned driver.
- [ ] Stop and restart one secondary and verify the set recovers.
- [ ] Stop the current primary, wait for an election, and verify the client reconnects.
- [ ] Verify that the runner can reach a selected member when a direct stale-member read is needed.
- [ ] Record any limitation that prevents separate client and replication paths.

### Harness check

- [ ] A fresh `make setup` reaches the expected topology repeatedly.
- [ ] Reset and teardown do not depend on manual Docker Desktop actions.
- [ ] Secondary failure and primary election appear as structured events.
- [ ] The setup can be recreated without undocumented changes.

## 4. Define data and logical history

- [ ] Choose a small document model that represents ordered writes, observed versions, and dependent writes.
- [ ] Define the logical version type and comparison rule.
- [ ] Put the key and logical version/value in every read and write record.
- [ ] Put client program order in every relevant record, for example with `client_sequence`.
- [ ] Put causal dependencies in the record. For WFR, store a value such as `y.source_version = v_r`.
- [ ] Do not use wall-clock time, ObjectId order, or response order as the version order.
- [ ] Define how MW visibility will be observed and tested.
- [ ] Define how the checker handles an incomplete, malformed, or truncated history.
- [ ] Define a canonical serialization and hash for a history.

## 5. Record operations and faults

- [ ] Write one structured record for every attempted operation.
- [ ] Include `trial_id`, `client_id`, `session_id`, and `operation_id`.
- [ ] Include `operation_type`, `key`, and `value_or_version`.
- [ ] Include `target_node`, read concern, write concern, and causal-session state.
- [ ] Include invocation time, response time, success/error, and dependency metadata.
- [ ] Add client sequence, fault event ID, member ID/role, error code, timeout type, runner ID, and container image when available.
- [ ] Flush records during faults so a failed process does not silently lose the history prefix.
- [ ] Record fault start and end events under the same trial ID.
- [ ] Store the settings manifest and software versions beside each raw history.
- [ ] Validate records when they are written and during replay.
- [ ] Add one small fixture for each property and each result label.

## 6. Write the checkers before the workloads

- [ ] Add a strict parser that reports missing fields, duplicate IDs, bad dependencies, and unknown schema versions.
- [ ] RYW: after `W_c(x, v_w)` and a later `R_c(x, v_r)`, report a counterexample when `v_r < v_w`.
- [ ] MR: for successive reads `R_i(x, v_i)` and `R_(i+1)(x, v_j)`, report a counterexample when `v_j < v_i`.
- [ ] MW: for two ordered writes, use the documented visibility rule to find a missing predecessor.
- [ ] WFR: use the recorded read dependency and the documented visibility rule for the dependent write.
- [ ] Return the trial, operations, versions, members, setting, and fault event for every finding.
- [ ] Return exactly `PASS`, `VIOLATION`, or `UNAVAILABLE` for each target history.
- [ ] Keep blocks, timeouts, and errors before a successful history out of the violation count.
- [ ] Add valid and counterexample fixtures for RYW, MR, MW, and WFR.
- [ ] Add tests that prove an unavailable operation is not a violation.
- [ ] Add malformed-history tests that fail with a useful error.
- [ ] Add replay tests showing identical output for identical canonical input.

### Checker check

- [ ] Checker tests pass without MongoDB.
- [ ] Each predicate produces inspectable evidence from a fixture.
- [ ] The denominator for every rate is clear from the checker output.
- [ ] The checker does not import or call the workload generator.

## 7. Build four adversarial workloads

- [ ] Implement one small deterministic workload for each property before adding random or high-rate traffic.
- [ ] RYW: write a version, then read the same key through the selected client/session path.
- [ ] MR: read a version, then attempt a later read from a replica state that could be older.
- [ ] MW: issue two ordered writes and inspect the visibility of the second and its predecessor.
- [ ] WFR: read a version, attach it to a dependent write, and inspect the dependent result.
- [ ] Add a valid control history for each workload.
- [ ] Make IDs and client sequences deterministic for a fixed seed.
- [ ] Keep workload generation separate from checking and aggregation.
- [ ] Set a timeout and a bounded stop rule for a blocked operation.
- [ ] Save workload parameters and seed in the trial manifest.

## 8. Write predictions before the campaign

- [ ] Create the prediction matrix before running the main campaign.
- [ ] For every selected setting, fault case, and property, record the expected result and the theory behind it.
- [ ] Review C1: `local` / `w: 1` / causal off.
- [ ] Review C2: `local` / `w: 1` / causal on.
- [ ] Review C3: `majority` / `w: 1` / causal on.
- [ ] Review C4: `local` / `majority` / causal on.
- [ ] Review C5: `majority` / `majority` / causal off.
- [ ] Review C6: `majority` / `majority` / causal on.
- [ ] Remove redundant settings from the main campaign only with a written reason.
- [ ] Record the normal, secondary-failure, primary-failure/election, and partition schedule for each selected setting.
- [ ] Record the repetition count and schedule order.
- [ ] Commit the matrix before committing the main result files.

### Prediction check

- [ ] The prediction matrix has a commit SHA older than the main result files.
- [ ] Analysis reads the matrix and cannot rewrite it from observations.

## 9. Add controlled faults

- [ ] Normal operation: record baseline availability and latency.
- [ ] Secondary failure: stop one secondary, record the event, run the workload, and restore the member.
- [ ] Primary failure: identify the primary, stop it, record election and temporary unavailability, wait for recovery, and run post-election checks.
- [ ] Network partition: keep the member alive and block the documented client or replication path.
- [ ] Verify that the partition test can reach a stale member if that is part of the question.
- [ ] If it cannot, mark the test unsupported and change the topology. Do not call a node crash a partition.
- [ ] Record the fault interval for diagnosis. Do not use it as a consistency predicate.
- [ ] Add latency, packet loss, or bandwidth limits only after writing the question and prediction they support.
- [ ] Clean up and verify the replica set after every fault.

## 10. Run the pilot

- [ ] Run all checker fixtures.
- [ ] Run one normal-operation trial for every selected setting.
- [ ] Run one bounded secondary-failure trial.
- [ ] Run one primary-election trial.
- [ ] Run one partition trial and inspect whether the intended stale path exists.
- [ ] Check every raw record against the schema.
- [ ] Check that each history links to its setting, software manifest, and fault events.
- [ ] Inspect at least one trace for each property and one unavailable operation.
- [ ] Delete derived summaries and figures, then rebuild them from raw histories.
- [ ] Fix schema, topology, and instrumentation issues before the main campaign.
- [ ] Freeze the versions, settings, workloads, repetitions, and fault schedules used by the pilot.

### Pilot check

- [ ] The pilot produces raw history, checker output, manifest, and summary.
- [ ] No member identity, fault event, required field, or result label is missing.
- [ ] The partition path is verified or clearly marked unsupported.

## 11. Run the main campaign

- [ ] Give every setting, fault case, property, and repetition a unique trial ID.
- [ ] Reset the database before every independent trial.
- [ ] Record code, settings, checker, server, driver, and container versions.
- [ ] Run the registered normal-operation cases.
- [ ] Run the registered secondary-failure cases.
- [ ] Run the registered primary-failure/election cases.
- [ ] Run the registered network-partition cases.
- [ ] Keep raw histories for failed and unavailable trials.
- [ ] Separate infrastructure failures from valid unavailable operations.
- [ ] Watch disk space and file integrity without dropping traces.
- [ ] Retry a failed trial only under a written rule. Never overwrite the original record.
- [ ] Start a new campaign if settings or schedules change after the first main run.

### Optional five-member comparison

- [ ] Start only after the three-member campaign passes all checkpoints.
- [ ] Repeat setup, election, failure, partition, and checker smoke tests.
- [ ] Keep five-member results separate and state the question they answer.

## 12. Analyse raw histories

- [ ] Make `make analyse` read histories and manifests without a live MongoDB connection.
- [ ] Validate and canonicalize every input history before aggregation.
- [ ] Compute violation rate as `violating successful histories / successful histories`.
- [ ] Compute availability rate as `successful operations / attempted operations`.
- [ ] Show PASS, VIOLATION, and UNAVAILABLE counts for every setting, fault case, and property.
- [ ] Report p50, p95, and p99 latency. Add the mean only as a secondary number.
- [ ] Measure recovery after a recorded primary failure/election.
- [ ] Generate the settings x fault cases x properties result table or heatmap.
- [ ] Generate a counterexample trace for every real violation discussed in the report.
- [ ] Generate a prediction, observation, and explanation table.
- [ ] Record repetitions, workload, fault schedule, and software versions behind every aggregate.
- [ ] Stop on contradictory or impossible records and report the cause.
- [ ] Describe zero violations as an observation under the tested schedule, never as a universal proof.
- [ ] Keep normal operation, secondary failure, election, and partition results separate.

### Analysis check

- [ ] Every reported number maps to raw histories and a checker version.
- [ ] Re-running analysis with the same inputs gives the same output or a documented equivalent.
- [ ] Every table denominator and result label is unambiguous.

## 13. Write the report

- [ ] Write the abstract after the analysis is fixed.
- [ ] State the research questions and the contribution.
- [ ] Define RYW, MR, MW, WFR, logical versions, causal precedence, and the MongoDB settings used.
- [ ] Describe the topology, exact versions, installation, reset, and fault controls.
- [ ] Include the prediction matrix and the reason for each setting.
- [ ] Describe workloads, history fields, predicates, controls, repetitions, and faults.
- [ ] Present results with separate PASS, VIOLATION, and UNAVAILABLE counts, availability, latency, and recovery.
- [ ] Explain representative histories and the system behaviour that produced them.
- [ ] State the limits of one-host containers, synthetic network faults, timing variation, finite trials, small workloads, and version scope.
- [ ] Document `make setup`, `make experiment`, and `make analyse`.
- [ ] Add the conclusion with claims limited to the tested system.
- [ ] Cite course lectures, MongoDB, PyMongo, fault-injection tools, and the required AI-use disclosure.
- [ ] Add the appendix with Compose/setup commands, fault scripts, selected traces, checker pseudocode, and extra plots.
- [ ] Add the architecture, counterexample, fault-topology, result, violation-trace, trade-off, and latency figures.
- [ ] Add the properties, versions, predictions, controls, results, explanation, and limits tables.
- [ ] Leave installation screenshots out unless one proves an experimental observation.

## 14. Reproduce and submit

- [ ] Test from a fresh checkout on the documented host.
- [ ] Run the command sequence without undocumented manual edits.
- [ ] Check that teardown and reset leave no hidden state.
- [ ] Check that all code, scripts, tests, and short reproduction instructions are present.
- [ ] Check that references match the versions and tools actually used.
- [ ] Check that every trace used by the report is readable and linked to its summary and figure.
- [ ] Open the final PDF and inspect figures, tables, and placeholder text.
- [ ] Add the required disclosure of any AI or generative tools used.
- [ ] Record the final commit SHA and the commands used to build the submitted files.
- [ ] Submit the PDF and reproduction package to Canvas by 27 September 2026.
- [ ] Keep a copy of the final raw-data, summary, figure, and report manifests if the course permits it.

## 15. Later work only with a clear question

- [ ] Five-member replica set.
- [ ] Controlled latency.
- [ ] Packet loss.
- [ ] Bandwidth limits.
- [ ] Extra orchestration or monitoring services.

For any later item, write the question, prediction, fault schedule, and analysis plan first.
