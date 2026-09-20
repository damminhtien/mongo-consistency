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
- RQ2: How do normal operation, secondary failure, primary failure, and network
  partition affect consistency outcomes, availability, and recovery?
- RQ3: Which observed routing, session-time, and topology evidence explains
  representative outcomes? Internal MongoDB mechanisms are not claimed unless
  the recorded trace supports the inference.
- RQ4: What consistency, operation-success, completion, latency, election, and
  recovery trade-offs appear in the recorded campaigns?

RQ1 uses stale-replica and election schedules to expose the required states.
RQ2 separately compares topology conditions. This boundary prevents a change in
configuration and a change in failure model from being treated as one factor.

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
2. Finish direct topology observation, routing telemetry, and setup-version
   provenance.
3. Verify controller isolation, client reachability, fault recovery, and
   post-heal convergence.
4. Validate the four schedules with checker fixtures and a 32-history smoke.
5. Freeze predictions and protocol commits.
6. Run the 192-history pilot; block main runs on harness errors, wrong routing,
   cleanup failures, or more than 5% precondition misses for any property.
7. Run RQ1 sequentially: 320 normal controls plus 960 adversarial histories.
8. Run RQ2 on its representative configurations and four topology conditions.
9. Rebuild analysis, plots, LaTeX macros, PDF, and reproduction archive from raw
   histories.
10. Audit the clean-clone path, generated artifacts, staged diff, and final
    submission package.

## Completion gates

The work is complete only when all requirements in
[experimental-protocol.md](experimental-protocol.md) have current evidence:
fixture and malformed-history tests; exact property schedules; live smoke;
frozen provenance; clean pilot; complete RQ1 and RQ2 manifests; offline analysis;
rendered and inspected PDF; reproducible archive; and CI checks. A passing unit
suite does not substitute for live schedule evidence.

make smoke, make pilot, make experiment, and make rq2 require a running Docker
daemon. If Docker is unavailable, continue with offline implementation
and tests, but do not describe live validation as complete.
