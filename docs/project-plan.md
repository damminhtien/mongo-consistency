# Project plan

Source: [DSA5208 Scalable Distributed GRP Project](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), read on 15 September 2026. The Google Doc contains the agreed direction for this project.

## Locked decisions

Recorded on 15 September 2026:

- The group has three members: DAM MINH TIEN (A0355091E), NGUYEN MINH DUC (Student ID pending), and VU NHAT MINH THU (Student ID pending).
- MongoDB is the final database choice for this project.
- The baseline is a three-member, data-bearing MongoDB replica set run with Docker Compose.
- The selected toolchain is MongoDB server, PyMongo, Python, Docker Engine, and Docker Compose.

The exact numeric versions are still an execution prerequisite. Record them after the environment and Compose image are fixed, then copy them into every trial manifest.

## System choice

Use a MongoDB replica set with Python, PyMongo, and Docker Compose.

### Why MongoDB

- It gives the project a direct way to study RYW, MR, MW, and WFR.
- The DSA5208 material connects these properties with read concern, write concern, majority snapshots, causal sessions, and Lamport clocks.
- It offers useful configuration differences without requiring a large deployment.
- Cassandra and ScyllaDB would require more work to relate quorum settings to all four properties.
- CockroachDB and Google Spanner offer fewer weak-versus-strong contrasts for this particular study.

### Deployment

- Start with three data-bearing containers in one replica set.
- Consider five members only after the three-member setup has passed the tests in `TODO.md`.
- Use Docker Compose. Kubernetes and Docker Swarm are not part of the baseline.
- Use Docker networking and, if needed, `tc`/`netem` or `iptables` for faults.

Keep the deployment small. The time should go into the workloads and the analysis rather than into orchestration.

## Working hypothesis

The observed client-centric behaviour depends on read concern, write concern, causal session ordering, and the failure condition. Replication strength by itself does not describe the complete client history.

Weak settings may expose stale or causally invalid histories when replicas lag or the network is partitioned. Stronger causal settings may preserve ordering by waiting, blocking, rejecting, or timing out instead of returning an invalid history.

```text
observed client consistency
  = f(read concern, write concern, causal session, failure condition)
```

Measure the trade-off between fewer inconsistent histories and the possible cost in latency or availability during a failure. Treat this as a hypothesis until the tests provide evidence.

## Research questions

- **RQ0:** How do MongoDB settings and failure conditions affect RYW, MR, MW, and WFR as seen by an application?
- **RQ1:** How do read concern, write concern, and causal sessions change those four properties?
- **RQ2:** Does behaviour seen during normal operation change after a node failure or network partition?
- **RQ3:** Which mechanisms stop a client from reading causally older state under stronger settings?
- **RQ4:** What consistency, availability, and latency trade-off appears during failure?

Main comparison: is majority replication enough for client-centric causal consistency, or does causal session state add another ordering constraint?

## Variables and settings

### Variables

- Read concern: `local` or `majority`.
- Write concern: `w: 1` or `majority`.
- Causal session: enabled or disabled.
- Failure condition: normal operation, secondary failure, primary failure and election, or network partition.
- Optional replication delay or packet loss, only when it answers a specific stale-state question.

### Candidate settings

| ID | Read concern | Write concern | Causal session |
| --- | --- | --- | --- |
| C1 | `local` | `w: 1` | off |
| C2 | `local` | `w: 1` | on |
| C3 | `majority` | `w: 1` | on |
| C4 | `local` | `majority` | on |
| C5 | `majority` | `majority` | off |
| C6 | `majority` | `majority` | on |

Do not run every possible combination by default. Pick settings that lead to different predictions and explain the choice. Complete the prediction matrix before looking at the main results.

## Method

Use short histories designed to find a counterexample to one property. A final value check is not enough.

The program has five parts:

1. workload generator;
2. MongoDB cluster;
3. operation history recorder;
4. offline consistency checkers; and
5. metrics and plots.

Keep the generator separate from the checker. The checker must be able to analyse a saved history without running the workload again.

The event fields and predicates are in [experimental-protocol.md](experimental-protocol.md).

## Fault cases

- **Normal operation:** measure the baseline behaviour and latency.
- **Secondary failure:** stop one secondary and record availability and any change in the prediction.
- **Primary failure:** stop the current primary and record the election, temporary write failures, recovery time, and post-election behaviour.
- **Network partition:** keep the isolated member running so it can become stale. Record which client and replication paths are blocked.
- **Optional faults:** add latency, packet loss, or bandwidth limits only when the change answers a written question.

The partition test should let the runner query a stale member while replication traffic to that member is blocked. If the network layout cannot provide those two paths separately, record the limitation and change the test rather than calling a node crash a partition.

## Metrics

- Violation rate: `violating successful histories / successful histories`.
- Availability rate: `successful operations / attempted operations`.
- Latency: p50, p95, and p99, plus the mean when it helps explain a result.
- Recovery: time from primary failure to usable post-election service.

An operation that blocks, times out, or fails before producing a successful history is `UNAVAILABLE`. It is not a consistency violation.

No observed violation is a result of the tested workload and fault schedule. It is not proof of a universal guarantee.

## Report

Report title:

> Experimental Evaluation of Client-Centric Consistency in MongoDB under Replica Failures and Network Partitions

For each result, show the question, theory, prediction, experiment, recorded history, checker output, and explanation. The full report outline is in [report-plan.md](report-plan.md).

## Limits

- Three containers on one laptop are logical replicas, not three independent machines.
- Docker network faults are synthetic and do not reproduce a real WAN.
- A finite set of trials cannot establish a universal guarantee.
- Scheduler and replication timing can vary. Repeat trials and retain the raw histories.
- Small synthetic workloads improve causal isolation but limit external validity.
- Conclusions apply only to the MongoDB and PyMongo versions and settings that were tested.

## Reproduction target

```bash
make setup
make experiment
make analyse
```

Expected output directories:

```text
results/raw/       raw operation histories
results/summary/   aggregated metrics
figures/           generated plots
scripts/           setup and fault scripts
src/               runner and checkers
tests/             checker tests
```

Use MongoDB documentation and course theory to define the expected behaviour. Use the experiments to look for counterexamples and measure availability and latency.

## Sources

- DSA5208 Lecture 1: physical and logical time, causal precedence, and Lamport clocks.
- DSA5208 Lecture 3: the four client-centric models, MongoDB read/write concern, majority snapshots, and causal sessions.
- MongoDB documentation for causal consistency, read concern, write concern, replica sets, elections, and the server version used.
- PyMongo documentation for sessions and read/write concern APIs.
- Documentation for each fault-injection tool used.
- The AI-use disclosure required by the course.
