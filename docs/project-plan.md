# Project plan

This plan follows the DSA5208 project brief, the local course slides, and the methodology review accepted on 15 September 2026.

Slide sources kept outside the repository:

- `Lec0.pdf`, page 16: replicated database project.
- `Lec1.pdf`, pages 17-20: logical clocks and Lamport scalar time.
- `Lec3.pdf`, pages 37-44: definitions and counterexample histories for RYW, WFR, MR, and MW.
- `Lec3.pdf`, pages 47-53: MongoDB primary/secondary routing, read and write concerns, majority snapshots, and causal sessions.

## Locked decisions

- Database: MongoDB replica set.
- Deployment: three data-bearing members in Docker Compose.
- Toolchain target: MongoDB 7.0.34, PyMongo 4.18.1, Python 3.14.7, Docker Engine 29.8.0, and Docker Compose 5.5.1.
- The MongoDB and Docker pins were revised on 16 September 2026. MongoDB 8.0.32 failed its startup guard on the Docker Desktop kernel available on this machine; `mongo:7.0.34` passed a direct startup probe, and the setup record retains the actual image digest and runtime versions.
- The actual versions and MongoDB image digest must be verified by `make setup` and copied into every trial manifest.
- Configuration matrix: all eight C1-C8 cells.
- RQ1 uses faults as instruments for creating adversarial replica states. RQ2 is the separate comparison of failure conditions.
- Workload client: one logical writer and one explicit PyMongo session per trial.
- Both causal ON and causal OFF use an explicit session; only `causal_consistency` changes.
- Read routing: tagged secondary members for stale and fresh reads.
- Write routing: one seed list and driver-selected primary.
- Retries: disabled for reads and writes.
- Raw histories and derived summaries are retained with manifests and hashes.
- Versioned JSON contracts cover manifests, operations, fault events, histories,
  outcomes, and analysis summaries. `make check-schemas` validates them without
  requiring a live database.

## Research questions

- RQ0: How do MongoDB settings and failure conditions affect the four client-centric properties observed by an application?
- RQ1: How do read concern, write concern, and causal sessions change RYW, MR, MW, and WFR?
- RQ2: Does behaviour seen during normal operation change after a node failure or network partition?
- RQ3: Which MongoDB mechanisms prevent causally older observations under stronger settings?
- RQ4: What consistency, availability, and latency trade-off appears during failure?

RQ1 reports configuration effects. Its stale-secondary and election schedules are test instruments, not a claim that configuration, failure type, and property form one undifferentiated research factor. RQ2 will compare normal operation, secondary failure, primary election, and network partition as topology conditions.

## Prediction matrix

The matrix is frozen before the main result files are created. A guaranteed cell means that a successful violating history is not expected under the documented semantics. An unguaranteed cell means that the schedule may expose a counterexample; it does not predict a violation on every repetition.

| ID | Read concern | Write concern | Causal session | Properties with a documented guarantee target |
| --- | --- | --- | --- | --- |
| C1 | `local` | `w:1` | off | none |
| C2 | `local` | `w:1` | on | none |
| C3 | `majority` | `w:1` | on | MR, WFR |
| C4 | `local` | `majority` | on | MW |
| C5 | `majority` | `majority` | off | none assumed |
| C6 | `majority` | `majority` | on | RYW, MR, MW, WFR |
| C7 | `majority` | `w:1` | off | none |
| C8 | `local` | `majority` | off | none |

C7 and C8 complete the factorial design with explicit ablation questions. They are not an unmotivated expansion of the matrix.

## Logical history

Each trial creates a unique namespace and one logical document `x`. The document stores an append-only list of updates. Each update contains an application version, a write ID, the effect written by the operation, and explicit dependency fields.

The version is an integer allocated by the single logical writer for that trial. It is not a Lamport timestamp. Client sequence numbers and dependency IDs define the tested history. MongoDB server timestamps and cluster metadata are recorded for diagnosis only.

MW and WFR must operate on the same logical item `x`. A different-key history is invalid for these Lecture 3 predicates.

The observer reads the entire logical document in one query. For MW, W2 visible without W1 is a counterexample. For WFR, a dependent W2 visible without the version read by R1 is a counterexample.

## Property schedules

- RYW: isolate replication traffic to one tagged secondary, write a new version through the primary, then read the same key from the stale secondary.
- MR: isolate one secondary, read a fresh version from another secondary, then read from the stale secondary.
- MW: complete W1 on the old primary, create the partition, wait for the majority side to elect a new primary, complete W2 on the new primary, and inspect one snapshot of `x`.
- WFR: read a version on the old side before the partition, complete a dependent write on the new primary after election, and inspect one snapshot of the same `x`.
- Normal control: run the same four short histories without an injected fault.

The runner never uses the generic sequence `partition -> wait for election -> run all workloads`. Each property has its own registered barriers, fault interval, operation order, and cleanup step.

The Compose topology provides separate client and replica paths. The fault controller can block member replication traffic while the runner still reaches a selected stale member. If that precondition cannot be established, the trial is `UNSUPPORTED` and is excluded from database metrics.

## Campaign

- Pilot: five adversarial repetitions for each of the 32 configuration/property cells, plus one normal smoke trial for each cell.
- Normal main baseline: `8 x 4 x 10 = 320` histories.
- Adversarial main campaign: `8 x 4 x 30 = 960` histories.
- Main total: 1,280 histories.
- Each case receives a unique trial ID and namespace.
- Case order is a seeded, stratified shuffle using `20260915 + campaign_ordinal`.
- A trial is never overwritten. A rerun receives a new trial ID and retains the old trace.

The main campaign uses 30 repetitions for repeatability of registered histories, not as a universal probability estimate. If the pilot shows that a valid schedule remains timing-dependent, run a separate 100-repetition extension only for the affected key cells and report Wilson intervals.

## Timeouts and outcomes

- Connection timeout: 2 seconds.
- Server selection timeout: 5 seconds.
- Operation/socket deadline: 5 seconds.
- `wtimeoutMS`: 5 seconds where applicable.
- Topology convergence and election barrier: 30 seconds.
- Subtrial deadline: 60 seconds.

History outcomes are `PASS`, `VIOLATION`, `UNAVAILABLE`, and `INDETERMINATE`. `HARNESS_ERROR` and `UNSUPPORTED` are infrastructure classifications and do not enter database metrics. A write timeout or lost write response is `INDETERMINATE` because the mutation may have occurred.

## Factorial analysis

For each property and schedule, compute consistency, completion, availability, and latency separately. Report the main effects of read concern, write concern, and causal session, plus `RC x CS`, `WC x CS`, `RC x WC`, and the three-way interaction where valid.

For a metric `Y`:

```text
Delta_CS(rc,wc) = Y(rc,wc,on) - Y(rc,wc,off)
Delta_RC(wc,cs) = Y(majority,wc,cs) - Y(local,wc,cs)
Delta_WC(rc,cs) = Y(rc,majority,cs) - Y(rc,w:1,cs)
```

Consistency violation rate is `VIOLATION / (PASS + VIOLATION)`. Operation success rate and history completion rate are reported separately. `UNAVAILABLE`, `INDETERMINATE`, `HARNESS_ERROR`, and `UNSUPPORTED` remain visible in their own counts.

## Reproduction and report

The implementation provides:

```bash
make setup
make pilot
make experiment
make analyse
make submission
```

`make analyse` reads canonical raw histories and manifests without a live MongoDB connection. It computes outcome counts, rates, operation latency, election and recovery intervals, and factorial contrasts for the registered metrics. The report follows the sequence question, theory, prediction, adversarial experiment, recorded history, checker, and explanation. It includes the four slide-style property histories, architecture and fault diagrams, prediction matrix, factorial contrasts, outcome counts, representative traces, latency, recovery, limitations, and AI-use disclosure.

## Limits

- Three containers on one laptop are logical replicas, not independent machines.
- Docker network isolation is a synthetic fault, not a real WAN.
- A finite campaign cannot prove a universal guarantee.
- MongoDB internal causal metadata is inferred only through observable behaviour; it is not directly inspected by the runner.
- Conclusions apply only to the recorded software versions, image digest, topology, schedules, and workloads.
