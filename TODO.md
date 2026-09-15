# Work list

The project is complete when the documents, implementation, raw histories, analysis, and submission package satisfy the protocol.

## Documents

- [x] Record group members and pending student IDs.
- [x] Record MongoDB, PyMongo, Python, Docker, and Compose targets.
- [x] Record the C1-C8 configuration matrix and prediction boundary.
- [x] Map the plan to the DSA5208 slide pages.
- [x] Record review fixes for schedules, logical versions, outcomes, sessions, timeouts, order, namespaces, and factorial analysis.
- [x] Define the LaTeX report sections, figures, tables, and evidence rules.
- [x] Keep `.codex/` and `AGENTS*` ignored and untracked.

## Configuration and schema

- [x] Add machine-readable C1-C8 configurations.
- [x] Add the frozen prediction manifest before any result files.
- [x] Add schedule definitions for RYW, MR, MW, WFR, and normal control.
- [ ] Add schemas for manifests, operations, fault events, histories, outcomes, and summaries.
- [x] Add version and image-digest capture.

## Checkers and fixtures

- [x] Implement canonical history serialization and SHA-256 hashing.
- [x] Implement separate RYW, MR, MW, and WFR checkers.
- [x] Reject different-key MW and WFR histories.
- [x] Distinguish PASS, VIOLATION, UNAVAILABLE, INDETERMINATE, HARNESS_ERROR, and UNSUPPORTED.
- [x] Add valid, violating, unavailable, ambiguous, malformed, and dependency fixtures.

## Compose harness

- [x] Build the three-member MongoDB 8.0.32 replica-set Compose stack.
- [x] Keep client and replica network paths separate.
- [x] Add health checks, replica-set initialization, and stable-topology verification.
- [ ] Add a fault controller without Docker socket, credentials, or unrelated host mounts.
- [ ] Capture actual command routing with PyMongo monitoring.
- [ ] Verify stale-member client access through replication-path isolation.
- [ ] Verify election barrier independence from operation timeout.
- [ ] Verify fault cleanup before the next trial.

## Workloads and runner

- [ ] Implement explicit causal ON and causal OFF sessions.
- [ ] Disable retry reads and retry writes.
- [ ] Implement unique namespaces and integer application versions.
- [ ] Record parent and read dependencies on the same logical document `x`.
- [ ] Implement seeded stratified campaign order.
- [ ] Implement pilot, normal baseline, and adversarial campaign modes.
- [ ] Preserve raw histories, manifests, fault events, errors, and hashes.

## Tests and gates

- [ ] Run all checker fixtures.
- [ ] Run malformed and ambiguous history tests.
- [ ] Run routing, topology, fault cleanup, and runner-isolation tests.
- [ ] Run `make test`.
- [ ] Run `make check-docs`.
- [ ] Run `make setup`.

## Campaign and analysis

- [ ] Run the pilot and review preconditions.
- [ ] Freeze the prediction commit before main result files.
- [ ] Run 320 normal histories.
- [ ] Run 960 adversarial histories.
- [ ] Run offline checker and summary generation.
- [ ] Compute outcome counts, rates, completion, latency, recovery, main effects, and interactions.
- [ ] Generate all required tables, figures, and four property timelines.

## Submission

- [ ] Render and inspect the final PDF.
- [ ] Check the PDF and LaTeX source for documentation rules.
- [ ] Build the reproduction archive with `make submission`.
- [ ] Confirm agentic metadata, caches, credentials, and unrelated artifacts are absent.
- [ ] Inspect staged diffs and run `git diff --check`.
- [ ] Commit each coherent implementation slice and push `main`.
