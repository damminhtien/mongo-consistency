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
- Raw histories are the only input to offline summaries, figures, and report
  macros.

## Research questions

- RQ1: How do read concern, write concern, and causal sessions affect RYW, MR,
  MW, and WFR?
- RQ2: How do node failures, primary failover, and network partitions affect
  client-centric consistency, operation availability, and recovery in a
  three-member MongoDB replica set?
- RQ3: Which observed routing, session-time, and topology evidence explains
  representative outcomes? Internal MongoDB mechanisms are not claimed unless
  the recorded trace supports the inference.
- RQ4: What consistency, operation-success, completion, latency, election, and
  recovery trade-offs appear in the recorded campaigns?

RQ1 uses stale-replica and election schedules to expose the required states.
RQ2 separately compares topology conditions. This boundary prevents a change in
configuration and a change in failure model from being treated as one factor.
RQ2 uses C1, C3, C4, and C6 for all four properties. It compares secondary
crash, primary crash/election, and primary-isolating network partition. It
reuses the matching RQ1 normal histories as its descriptive baseline and adds
no normal histories of its own. C5 versus C6 is reserved for RQ3.

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
4. Validate the four schedules with checker fixtures and a 32-history smoke.
5. Freeze predictions and protocol commits.
6. Run the 192-history pilot; block main runs on harness errors, wrong routing,
   cleanup failures, or more than 5% precondition misses for any property.
7. Run RQ1 sequentially: 320 normal controls plus 960 adversarial histories.
8. [x] Implement the grouped RQ2 runner and host coordinator, freeze their
   provenance, then run 384 core histories and 48 extra histories for four
   partition signature cells. The completed campaign has 432 histories across
   36 episodes; its generated report and detailed results are in
   [rq2-results.md](rq2-results.md) and the submission Results section.
9. Rebuild analysis, plots, LaTeX macros, PDF, and reproduction archive from raw
   histories.
10. Audit the clean-clone path, generated artifacts, staged diff, and final
    submission package.

## Completion gates

The RQ2 plan groups one fault episode by fault condition and repetition. Its
core is 24 episodes with 16 histories each; the partition extension is 12
episodes with four histories each. Report episode counts separately from
history counts so shared topology events are not presented as independent
fault events.

The work is complete only when all requirements in
[experimental-protocol.md](experimental-protocol.md) have current evidence:
fixture and malformed-history tests; exact property schedules; live smoke;
frozen provenance; clean pilot; complete RQ1 and RQ2 manifests; offline analysis;
rendered and inspected PDF; reproducible archive; and CI checks. A passing unit
suite does not substitute for live schedule evidence.

The grouped RQ2 campaign completed on 20 September 2026 from clean runner and
protocol commit `3c35e94`: 432 histories across 36 fault episodes, with all
faults verified and all recovery checks converged. Record/schema validation
passed and offline analysis was rebuilt. See [rq2-results.md](rq2-results.md).
The host coordinator applies faults outside the runner container through a
temporary IPC mount, keeping Docker control out of the runner.
