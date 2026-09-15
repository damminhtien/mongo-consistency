# Project plan

## Status and provenance

This is the agreed working plan transcribed from the project source document. It is a plan, not a result report. Predictions must be finalized before measurements are interpreted.

Source: [DSA5208 Scalable Distributed GRP Project](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), read 2026-09-15.

## 1. Technology and deployment decision

Use a MongoDB replica set with Python/PyMongo and Docker Compose.

### Rationale

- MongoDB maps directly to the four required client-centric models: RYW, MR, MW, and WFR.
- The planned course theory connects MongoDB read concern, write concern, majority-committed snapshots, causal sessions, and Lamport clocks to these models.
- MongoDB provides meaningful configuration contrasts without unnecessary infrastructure complexity.
- Cassandra and ScyllaDB are viable quorum-based alternatives, but mapping their semantics to all four properties creates a larger semantic and proof burden for this project.
- CockroachDB and Google Spanner are less suitable for this comparison because their default semantics are comparatively strong.

### Deployment scope

- Development baseline: three data-bearing MongoDB containers in one replica set.
- Optional final extension: five data-bearing members, only after the three-node harness is stable.
- Orchestration: Docker Compose. Kubernetes and Docker Swarm are out of scope unless orchestration becomes an explicit experimental variable.
- Fault control: Docker networking and, if needed, `tc`/`netem` or `iptables`.

The governing engineering priority is **experimental sophistication over deployment sophistication**: the deployment should be simple enough to reproduce and control node failures and partitions.

## 2. Central thesis

MongoDB's client-centric consistency behaviour depends on the interaction of read concern, write concern, causal session ordering, and failure conditions—not on replication strength alone.

Under weak configurations, replica lag and network partitions may expose stale or causally invalid client histories. Stronger causal configurations are expected to preserve the required ordering by waiting, blocking, rejecting, or timing out instead of returning an invalid history.

Working model:

```text
observed client consistency
  = f(read concern, write concern, causal session, failure condition)
```

The key trade-off to measure is:

```text
stronger consistency constraints
  -> fewer permitted inconsistent histories
  -> potentially higher latency and/or lower availability during failures
```

These are hypotheses to test, not guarantees established by this repository.

## 3. Research questions

- **RQ0:** How do MongoDB consistency configurations and failure conditions affect the four client-centric consistency guarantees observed by an application?
- **RQ1:** How do combinations of read concern, write concern, and causal sessions affect RYW, MR, MW, and WFR?
- **RQ2:** Do configurations that appear consistent during normal operation behave the same way under node failure and network partition?
- **RQ3:** What mechanisms prevent a client from observing causally older state when stronger MongoDB consistency settings are used?
- **RQ4:** What consistency–availability–latency trade-off is observed when the system preserves client-centric guarantees under failure?

Signature question: **Is majority replication alone sufficient for client-centric causal consistency, or does causal session state provide an additional ordering constraint?**

## 4. Experimental variables and configurations

### Independent variables

- read concern: `local` or `majority`;
- write concern: `w: 1` or `majority`;
- causal session: enabled or disabled;
- failure condition: normal operation, secondary failure, primary failure/election, or network partition; and
- optional controlled replication delay or packet loss, only if needed to make a specific stale-state question reproducible.

### Candidate configuration set

| ID | Read concern | Write concern | Causal session |
| --- | --- | --- | --- |
| C1 | `local` | `w: 1` | off |
| C2 | `local` | `w: 1` | on |
| C3 | `majority` | `w: 1` | on |
| C4 | `local` | `majority` | on |
| C5 | `majority` | `majority` | off |
| C6 | `majority` | `majority` | on |

Do not execute the full Cartesian product blindly. Select configurations that make distinct theoretical predictions and document why each selected configuration is included. The prediction matrix must be completed before result interpretation.

## 5. Experimental method

The core method is adversarial history construction, not random reads and writes followed by a final-value check. Each workload should deliberately try to falsify one predicted guarantee.

Planned pipeline:

```text
Workload Generator
  -> MongoDB Cluster
  -> Operation History Recorder
  -> Offline Consistency Checkers
  -> Metrics and Plots
```

Workload generation and history checking must remain separate so the checker is independent of the code that produced the history.

The detailed event schema, predicates, fault schedules, and outcome rules live in [experimental-protocol.md](experimental-protocol.md).

## 6. Fault plan

- **Normal operation:** establish baseline behaviour and latency.
- **Secondary failure:** stop one secondary; measure availability and whether the observed predictions change.
- **Primary failure:** stop the current primary; observe election, temporary write unavailability, recovery time, and post-election behaviour.
- **Network partition:** prefer a controlled partition over simply killing a node. Keep the partitioned member alive so it can retain stale state.
- **Optional advanced faults:** added latency, packet loss, or bandwidth restriction only when each answers a clear research question.

The preferred partition design lets the runner query a stale member when appropriate while replication traffic to that member is blocked. This may require separate client and replication network paths or selective firewall rules.

## 7. Metrics and interpretation

- **Violation rate:** `violating successful histories / successful histories`.
- **Availability rate:** `successful operations / attempted operations`.
- **Latency:** report at least p50, p95, and p99; do not rely only on the mean.
- **Recovery:** measure time to recover from primary failure/election when meaningful.

An unavailable operation is one that blocks, times out, or fails before producing a successful history. **UNAVAILABLE is not a consistency violation.**

Zero observed violations does not prove a universal guarantee. It means that no counterexample was observed under the tested workload and fault schedule.

## 8. Report strategy

Treat the submission as a small experimental-systems paper rather than an installation lab. The proposed title is:

> Experimental Evaluation of Client-Centric Consistency in MongoDB under Replica Failures and Network Partitions

The report should follow this evidence chain:

```text
Question -> Theory -> Prediction -> Adversarial Experiment
         -> Recorded History -> Checker -> Explanation
```

See [report-plan.md](report-plan.md) for the full section, figure, table, and reproducibility plan.

## 9. Explicit limitations

- Multiple containers on one laptop are logical replicas, not independent physical machines.
- Docker networking is not a real WAN; injected latency and partitions are synthetic.
- Finite experiments cannot prove universal guarantees.
- Scheduler and replication timing may be nondeterministic; repeat trials and retain exact histories.
- Synthetic minimal workloads improve causal isolation but reduce external validity.
- Conclusions must be scoped to the exact MongoDB and PyMongo versions and configurations tested.

## 10. Reproducibility target

Target command sequence:

```bash
make setup
make experiment
make analyse
```

Expected repository outputs:

```text
results/raw/       raw operation histories
results/summary/   aggregated metrics
figures/           generated plots
scripts/           setup and fault-injection scripts
src/               experiment runner and checkers
tests/             checker unit tests
```

The final methodological rule is to use MongoDB documentation and course theory to define expected guarantees, and use experiments to search for counterexamples and measure availability/latency consequences. Do not claim a guarantee merely because no violation was observed.

## 11. Planned sources

- DSA5208 Lecture 1: physical and logical times, causal precedence, and Lamport logical clocks.
- DSA5208 Lecture 3: consistency models, MongoDB read/write concerns, majority snapshots, and causal sessions.
- Official MongoDB documentation for causal consistency, read concern, write concern, replica sets, elections, and the exact server version used.
- PyMongo documentation for session and read/write concern APIs.
- Documentation for any fault-injection tool used.
- AI usage disclosure required by the assignment.
