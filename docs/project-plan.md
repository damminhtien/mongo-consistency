# Project plan

## Scope

Build a reproducible DSA5208 study of MongoDB client-visible consistency in a
three-member replica set. The authoritative experimental details live in
[experimental-protocol.md](experimental-protocol.md). This plan records the
research boundary, locked decisions, dependencies, and completion gates.

The slide references and how they shape the work are listed in
[slide-alignment.md](slide-alignment.md). The source PDFs remain outside the
repository.

## Decisions

- Final database: MongoDB replica set.
- Baseline deployment: three data-bearing members in Docker Compose.
- Target runtime: MongoDB 7.0.34, PyMongo 4.18.1, Python 3.14.7, Docker Engine
  29.8.0, Docker Compose 5.5.1. Setup must record actual versions and image
  digest for each campaign.
- Matrix: all eight combinations C1-C8 of read concern, write concern, and
  causal session.
- Prediction interpretation: durability-aware documented guarantee targets;
  contrasts across C1-C8 are exploratory and are not treated as a complete
  inferential factorial study.
- Client: one explicit ClientSession per trial for both causal ON and OFF.
- RYW, MR, MW, WFR, and normal control each have their own registered schedule.
- Every trial uses one unique logical document x, explicit integer versions,
  and recorded write/read dependencies.
- The final observer uses independent direct connections after healing.
- The canonical RQ1 campaign is sequential. No parallel latency is mixed into
  its measurements.
- Raw histories are the only input to offline summaries and figures.

## Research questions

- Q1: How do read concern, write concern, and causal sessions affect RYW, MR,
  MW, and WFR?
- Q2: How do node failures, primary failover, and network partitions affect
  client-centric consistency, operation completion, and recovery in a
  three-member MongoDB replica set?

Q1 uses stale-replica and election schedules to expose the required states.
Q2 separately compares topology conditions. This boundary prevents a change in
configuration and a change in failure model from being treated as one factor.
Q2 uses C1, C3, C4, and C6 for all four properties. It compares secondary
crash, primary crash/election, and primary-isolating network partition. It
reuses the matching Q1 normal histories as its descriptive baseline and adds
no normal histories of its own.

Selected matched histories provide mechanism evidence for the causal-session,
read-concern, and write-concern questions. The selected IDs and raw hashes live
in [`configs/rq3-anchors.json`](../configs/rq3-anchors.json); the selection
and its limits are described in
[`rq3-historical-trace-selection.md`](rq3-historical-trace-selection.md).
Three bounded matched contrasts replay each mechanism eight times (48 histories
total). These runs explain selected observations; they do not estimate
violation probabilities. A separate offline calculation records observed
definitive completion and latency consequences from immutable Q1 and Q2
histories. Both are supporting analyses for the two research questions.

## Configuration and predictions

| ID | RC | WC | Causal | Guarantee target |
| --- | --- | --- | --- | --- |
| C1 | local | w:1 | off | none |
| C2 | local | w:1 | on | none |
| C3 | majority | w:1 | on | MR, WFR |
| C4 | local | majority | on | MW |
| C5 | majority | majority | off | none assumed |
| C6 | majority | majority | on | RYW, MR, MW, WFR |
| C7 | majority | w:1 | off | none |
| C8 | local | majority | off | none |

These are predictions for successful histories under the protocol schedules. An
unguaranteed cell may pass or may expose a counterexample; it is not a
prediction that a violation must occur.

## Architecture

The runner coordinates four boundaries:

1. Subject client: one PyMongo session; emits only registered property
   operations.
2. Diagnostic layer: direct hello checks, setup writes, precondition
   observations, and independent post-heal reads.
3. Fault controller: topology changes only; no database writes and no Docker
   socket in the runner.
4. Offline checker: classifies serialized histories without a live database.

TopologyOracle is the source of role and stability observations. A topology is
stable only with one reachable primary and two reachable secondaries. Driver
cache state is logged separately and never treated as ground truth.

## Work sequence

1. Freeze the data model and schema for preconditions, operations, diagnostics,
   outcomes, and final observation.
2. [x] Finish direct topology observation, routing telemetry, and setup-version
   provenance. RQ2 live snapshots and per-operation routes are recorded in its
   provenance and raw histories.
3. [x] Verify controller isolation, client reachability, fault recovery, and
   post-heal convergence. All three targeted members were reached during F3
   replication isolation; all 36 episodes converged.
4. [ ] Re-run the 32-history smoke. The first run completed all histories but
   failed its gate on one MR seed-write precondition: the healthy secondary was
   still chained through the isolated member. The revised schedule verifies or
   corrects that sync source before issuing the seed write.
5. [x] Freeze predictions and protocol commits.
6. [ ] Run the 128-history pilot; block main runs on harness errors, wrong routing,
   cleanup failures, or more than 5% precondition misses for any property.
7. [ ] Re-run the reduced RQ1 after the MR schedule correction: 96 normal
   controls plus 160 adversarial histories, 256 in total.
8. [x] Implement and freeze the grouped RQ2 runner and host coordinator; run
   384 core histories and 48 F3 signature-extension histories. The current
   campaign completed 432 histories across 36 fault episodes.
9. [x] Verify the six historical Q1 anchors, complete ten topology rehearsals,
   run the 48-history mechanism replay, and analyze its control-valid pairs.
10. [ ] Re-derive consistency, observed definitive completion, indeterminate
    rate, and separate all-attempt and resolved critical-operation p50/p95
    latency from the refreshed RQ1 and existing RQ2 raw histories.
11. [ ] Rebuild machine-readable summaries and plots from canonical histories
    after the RQ1 rerun.
12. [ ] Audit the clean-clone path, generated numeric artifacts, staged diff,
    and the PDF plus runtime-code submission package.

## Completion gates

The RQ2 plan groups one fault episode by fault condition and repetition. Its
core is 24 episodes with 16 histories each; the partition extension is 12
episodes with four histories each. Report episode counts separately from
history counts so shared topology events are not presented as independent
fault events.

The previous 256-history RQ1 run and its derived summaries are retained, but
they are not final evidence after the MR schedule correction. The 432-history
RQ2 run and 48-history RQ3 run remain valid for their registered schedules.
RQ2 uses the corrected WFR schedule and write pre-image checker: its F3 WFR
cells contain 16 violations and 16 precondition misses. The RQ3 manifest
records eight control-valid pairs for each contrast.

The completion gate remains open. The 32-history smoke must pass before the
128-history pilot; RQ1 and its offline analyses then need to be rerun. The
clean-clone review, PDF/package build, and release checks also need current
evidence. Earlier records from superseded campaign semantics are excluded
from final counts.
The host coordinator applies faults outside the runner container through a
temporary IPC mount, keeping Docker control out of the runner.

## Client-visible consequence analysis

This is an offline analysis layer over the completed Q1 and Q2 histories. In
this checkout, RQ1 is stored under `results/raw/experiment` and RQ2 under
`results/raw/rq2`; the analyzer also accepts a `results/raw/rq1` alias. It
does not query MongoDB or modify raw records. For each configuration, scenario,
and property it reports the observed violation rate, observed definitive
completion rate, indeterminate rate, all-attempt p50/p95 latency of the
registered critical operation, and resolved p50/p95 latency for PASS and
VIOLATION histories. `UNAVAILABLE`, `INDETERMINATE`, precondition misses, and
harness errors remain separate. Valid timeout timestamps remain in the
all-attempt sample; the resolved sample has its own denominator.

For MW and WFR, PASS and VIOLATION use the course-definition evidence recorded
by the runner: W1's write ID in W2's atomic pre-image for MW and the
application version in that pre-image for WFR. Post-recovery observations are
reported separately as durability and rollback evidence.

The analysis writes `results/summary/rq4/metrics.csv`, `contrasts.csv`, and
`fault_deltas.csv`, plus three PDF figures under `figures/`. Rebuild it with
`make analyse RQ=rq4`. The main partition figure and table use the balanced
C1/C6 RYW and MW signature cells; no aggregate over the four properties is
used. C5/C6 partition cells remain missing in the contrast output rather than
being inferred.

## Current RQ1 status

The retained RQ1 campaign has 96 normal controls and 160 adversarial histories.
Its raw outcomes predate the MR replication-source control and must not be used
as final evidence. The replacement run will be written to
`results/raw/experiment` after the smoke and pilot gates pass.
