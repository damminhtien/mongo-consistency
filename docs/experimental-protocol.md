# Experimental protocol

This document turns the working plan into an implementation-facing protocol. It defines what must be recorded and how successful histories are classified. It does not prescribe results.

## 1. Experiment contract

Each trial must identify:

- one MongoDB configuration;
- one fault condition and fault schedule;
- one target consistency property, or one explicitly documented multi-property workload;
- one adversarial workload intended to find a counterexample; and
- the exact operation history returned by the system.

The checker must evaluate recorded logical versions and causal dependencies. Wall-clock order can describe invocation and response intervals, but must not be used as the consistency predicate itself.

## 2. Configuration record

Every trial records at least:

| Field | Meaning |
| --- | --- |
| `configuration_id` | One of C1–C6, or a documented extension. |
| `read_concern` | `local` or `majority`. |
| `write_concern` | `w: 1` or `majority`. |
| `causal_session` | Whether causal session ordering is enabled. |
| `mongodb_version` | Exact server version. |
| `pymongo_version` | Exact client library version. |
| `replica_set_members` | Member identities and roles for the trial. |
| `fault_condition` | Normal, secondary failure, primary failure/election, or partition. |

## 3. Operation-history schema

The recorder should emit one structured record per attempted operation. Required fields from the plan are:

```text
trial_id
client_id
session_id
operation_id
operation_type
key
value_or_version
target_node
read_concern
write_concern
causal_session_enabled
invocation_time
response_time
success_or_error
dependency_metadata
```

The value/version field must make the logical state observed or written explicit. For writes that depend on a prior read, encode the dependency directly; for example, a dependent write may carry `y.source_version = v_r`.

Recommended additional fields:

- `client_sequence` for per-client program order;
- `fault_event_id` and fault start/end markers;
- `replica_set_member_id` and observed role;
- `error_code` and timeout category;
- `history_hash` or canonical serialization version; and
- runner and container image identifiers.

Additional fields are useful only if they remain reproducible and do not replace the logical predicates below.

## 4. Formal checker predicates

Use the following planned logical versions. Let `W_c(x, v_w)` be a client write of version `v_w` for key `x`, and `R_c(x, v_r)` be a later client read returning version `v_r`.

### Read-your-writes (RYW)

If `W_c(x, v_w)` happens before a later `R_c(x, v_r)`, the history is a counterexample when:

```text
v_r < v_w
```

The checker must scope “later” to the recorded client/session ordering and retain enough metadata to explain the counterexample.

### Monotonic reads (MR)

For successive reads by one client, `R_i(x, v_i)` followed by `R_(i+1)(x, v_j)`, the history is a counterexample when:

```text
v_j < v_i
```

The predicate is about the client’s observed logical versions, not about response timestamps alone.

### Monotonic writes (MW)

For two writes by the same client with program order `W1 -> W2`, a counterexample exists if `W2` becomes visible while the causal predecessor `W1` is not yet visible in the required history.

The implementation must define and test the visibility observation used for this predicate before running the campaign. Do not substitute a wall-clock comparison for visibility.

### Writes-follow-reads (WFR)

If a client reads version `v_r` and then performs a dependent write, encode the dependency explicitly, for example:

```text
y.source_version = v_r
```

The history is a counterexample if the dependent write becomes visible in a state older than the version on which it depends.

## 5. Outcome classes

Every attempted history or operation must be classified as one of:

- **PASS:** a successful history satisfies the target predicate.
- **VIOLATION:** a successful history violates the target client-centric predicate.
- **UNAVAILABLE:** the operation blocks, times out, or fails before producing a successful history.

`UNAVAILABLE` is not a consistency violation. Summaries must keep these categories separate rather than treating an error or timeout as evidence of stale data.

## 6. Fault schedules

### Baseline

Run the adversarial workload under normal operation to establish baseline availability and latency.

### Secondary failure

Stop one secondary during or between selected operations. Record whether operations remain available and whether the observed behaviour differs from the pre-registered prediction.

### Primary failure and election

Stop the current primary. Record:

- the election interval;
- temporary write unavailability or errors;
- recovery time;
- the elected member; and
- post-election histories relevant to the target property.

### Network partition

Prefer a controlled partition that keeps the isolated member alive and stale. Record which client and replication paths are blocked. If the runner cannot query the stale member independently from the replication path, record that as a protocol limitation rather than claiming a stale-read test was performed.

### Optional network impairments

Add latency, packet loss, or bandwidth restriction only when it answers a clear question and the impairment is logged with the trial. These are optional; complexity alone is not a reason to add them.

## 7. Trial procedure

For each selected configuration and fault scenario:

1. Record software, replica-set, and fault-controller identities.
2. Initialize a clean, known logical state.
3. Start the operation-history recorder.
4. Apply the pre-registered fault schedule.
5. Run the adversarial workload.
6. Persist raw responses, errors, versions, dependencies, and fault events.
7. Run the offline checker independently of the workload generator.
8. Classify each target history as PASS, VIOLATION, or UNAVAILABLE.
9. Repeat the trial under the documented repetition count and schedule variation.
10. Generate aggregate summaries and preserve the raw trace used for every reported counterexample.

## 8. Required assertions and tests

The implementation should include unit tests for:

- an RYW counterexample and a valid RYW history;
- an MR counterexample and a valid MR history;
- an MW visibility-order counterexample and a valid order;
- a WFR dependency counterexample and a valid dependent write;
- unavailable operations that must not be counted as violations; and
- malformed or incomplete history records that fail closed with an explicit diagnostic.

The checker should be deterministic for a fixed canonical history. A result summary must be reproducible from `results/raw/` without connecting to MongoDB again.

## 9. Minimum analysis outputs

For every configuration × fault-condition × consistency-property cell, report where applicable:

- attempted operations;
- successful histories;
- PASS histories;
- VIOLATION histories;
- UNAVAILABLE operations;
- violation rate;
- availability rate;
- p50/p95/p99 latency; and
- recovery time for primary failure/election.

No cell should be presented as a universal guarantee. The report must include the workload, fault schedule, version scope, and number of repetitions behind the cell.
