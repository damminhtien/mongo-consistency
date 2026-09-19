# Work list

The project is complete when the documents, implementation, raw histories, analysis, and submission package satisfy the protocol.

## Documents

- [x] Record group members and pending student IDs.
- [x] Record MongoDB, PyMongo, Python, Docker, and Compose targets.
- [x] Record the C1-C8 configuration matrix and prediction boundary.
- [x] Map the plan to the DSA5208 slide pages.
- [x] Record review fixes for schedules, logical versions, outcomes, sessions, timeouts, order, namespaces, and factorial analysis.
- [x] Define the LaTeX report sections, figures, tables, and evidence rules.
- [x] Keep local tooling and generated metadata outside the source inputs.

## Configuration and schema

- [x] Add machine-readable C1-C8 configurations.
- [x] Add the frozen prediction manifest before any result files.
- [x] Add schedule definitions for RYW, MR, MW, WFR, and normal control.
- [x] Add schemas for manifests, operations, fault events, histories, outcomes, and summaries.
- [x] Add version and image-digest capture.

## Checkers and fixtures

- [x] Implement canonical history serialization and SHA-256 hashing.
- [x] Implement separate RYW, MR, MW, and WFR checkers.
- [x] Reject different-key MW and WFR histories.
- [x] Distinguish PASS, VIOLATION, UNAVAILABLE, INDETERMINATE, HARNESS_ERROR, and UNSUPPORTED.
- [x] Add valid, violating, unavailable, ambiguous, malformed, and dependency fixtures.

## Compose harness

- [x] Build the three-member MongoDB 7.0.34 replica-set Compose stack.
- [x] Keep client and replica network paths separate.
- [x] Add health checks, replica-set initialization, and stable-topology verification.
- [x] Add a fault controller without Docker socket, credentials, or unrelated host mounts.
- [x] Capture actual command routing with PyMongo monitoring.
- [x] Verify stale-member client access through replication-path isolation.
- [x] Verify election barrier independence from operation timeout.
- [x] Verify fault cleanup before the next trial.

## Workloads and runner

- [x] Implement explicit causal ON and causal OFF sessions.
- [x] Disable retry reads and retry writes.
- [x] Implement unique namespaces and integer application versions.
- [x] Record parent and read dependencies on the same logical document `x`.
- [x] Implement seeded stratified campaign order.
- [x] Implement pilot, normal baseline, and adversarial campaign modes.
- [x] Preserve raw histories, manifests, fault events, errors, and hashes.
- [x] Add atomic history/manifest writes, graceful shutdown, and deterministic campaign resume.
- [x] Add deterministic sharding across independent Compose replica sets with byte-checked merge.
- [x] Keep worker scratch results outside the canonical analysis tree.

## Tests and gates

- [x] Run all checker fixtures.
- [x] Run malformed and ambiguous history tests.
- [x] Run routing, topology, fault cleanup, and runner-isolation contract tests.
- [x] Run `make test`.
- [x] Run `make check-docs`.
- [x] Run `make check-schemas`.
- [x] Run `make setup` with the revised compatible MongoDB and Docker pins.

The original MongoDB 8.0.32 image stopped before startup on the current Docker
VM kernel. The revised `mongo:7.0.34` image passed a direct startup probe; live
setup now records a healthy three-member replica set and the resolved image
digest.

## Campaign and analysis

- [x] Run the pilot and review preconditions.
- [x] Freeze the prediction commit before main result files.
- [x] Run 320 normal histories.
- [x] Run 960 adversarial histories.
- [x] Run the main campaign with isolated parallel workers and confirm a complete canonical manifest.
- [x] Run offline checker and summary generation with an explicit `NO_DATA` state before the live campaign.
- [x] Implement outcome counts, rates, completion, latency, election, recovery, main effects, and interactions.
- [x] Generate all required tables, figures, and four property timelines from the complete raw-history set.

## Submission

- [x] Render and inspect the final PDF with the recorded campaign evidence.
- [x] Check the PDF and LaTeX source for documentation rules.
- [x] Build the reproduction archive with `make submission`.
- [x] Confirm local tooling, caches, credentials, and unrelated artifacts are absent from the package.
- [x] Inspect staged diffs and run `git diff --check`.
- [x] Commit each coherent implementation slice and push `main`.
