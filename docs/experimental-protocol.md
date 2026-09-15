# Experimental protocol

This protocol defines the records and checks used in each MongoDB trial. It does not assign results in advance.

## Trial record

Each trial has:

- one MongoDB setting;
- one fault condition and schedule;
- one target property, or a documented workload covering more than one property;
- one workload designed to find a counterexample; and
- the complete operation history returned by the system.

The checker uses logical versions and causal dependencies. Invocation and response times are recorded for latency and debugging, not as the consistency test.

## Configuration fields

Record these fields for every trial:

| Field | Meaning |
| --- | --- |
| `configuration_id` | C1 to C6, or a documented extension. |
| `read_concern` | `local` or `majority`. |
| `write_concern` | `w: 1` or `majority`. |
| `causal_session` | Whether causal session ordering is enabled. |
| `mongodb_version` | Exact server version. |
| `pymongo_version` | Exact client library version. |
| `replica_set_members` | Member identities and roles. |
| `fault_condition` | Normal, secondary failure, primary failure/election, or partition. |

## Operation history

Write one structured record for each attempted operation. The record contains:

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

The version field identifies the logical state read or written. A dependent write carries its read dependency, for example `y.source_version = v_r`.

Useful additional fields include:

- `client_sequence` for program order within one client;
- `fault_event_id` and fault start/end markers;
- `replica_set_member_id` and the observed role;
- `error_code` and timeout category;
- `history_hash` and serialization version; and
- runner and container image identifiers.

Use one canonical serialization so the same history produces the same checker input every time.

## Checker predicates

Let `W_c(x, v_w)` be a write of version `v_w` to key `x`, and let `R_c(x, v_r)` be a later read by the same client.

### Read-your-writes (RYW)

After `W_c(x, v_w)`, a later `R_c(x, v_r)` is a counterexample when:

```text
v_r < v_w
```

Use client or session order to define later. Include both operations and the relevant member details in the checker output.

### Monotonic reads (MR)

For successive reads `R_i(x, v_i)` and `R_(i+1)(x, v_j)` by one client, a counterexample has:

```text
v_j < v_i
```

Compare the logical versions returned to the client, not their response timestamps.

### Monotonic writes (MW)

For two writes by one client, flag a counterexample if the second write becomes visible while the required predecessor is not visible in the same history.

Before the campaign, specify how visibility is observed and test that rule with fixtures. Do not replace visibility with a wall-clock comparison.

### Writes-follow-reads (WFR)

After a client reads version `v_r`, its dependent write records the dependency, for example:

```text
y.source_version = v_r
```

Flag a counterexample if the dependent write becomes visible in a state older than the version it depends on.

## Result labels

Use three labels:

- **PASS:** the successful history satisfies the target property.
- **VIOLATION:** the successful history fails the target predicate.
- **UNAVAILABLE:** the operation blocks, times out, or fails before a successful history exists.

Do not count `UNAVAILABLE` as a consistency violation. Keep it in its own count in raw records and summaries.

## Fault schedules

### Normal operation

Run the workload without an injected fault. Record baseline availability and latency.

### Secondary failure

Stop one secondary during or between selected operations. Record whether operations continue and whether the result differs from the prediction.

### Primary failure and election

Stop the current primary. Record:

- the election interval;
- write errors or temporary unavailability;
- recovery time;
- the elected member; and
- histories after the election.

### Network partition

Keep the isolated member alive and record the client and replication paths that are blocked. If the runner cannot reach the stale member separately from the replication path, mark that test as unsupported and do not describe it as a stale-read experiment.

### Optional network changes

Add latency, packet loss, or bandwidth limits only when the change answers a named question. Log the setting with the trial.

## Trial procedure

For every selected setting and fault case:

1. Save software, replica-set, and fault-controller versions.
2. Reset the database to a known state.
3. Start the history recorder.
4. Apply the registered fault schedule.
5. Run the workload.
6. Save responses, errors, versions, dependencies, and fault events.
7. Run the checker without reconnecting to MongoDB.
8. Assign PASS, VIOLATION, or UNAVAILABLE.
9. Repeat using the registered repetition count and schedule.
10. Save the raw trace used for each number or counterexample in the report.

## Tests before the campaign

Add unit tests for:

- a valid RYW history and an RYW counterexample;
- a valid MR history and an MR counterexample;
- valid and invalid MW visibility order;
- a valid WFR dependency and a WFR counterexample;
- unavailable operations that must not count as violations; and
- malformed histories that produce an error instead of a false result.

For a fixed canonical history, checker output must be deterministic. The analysis command must work from `results/raw/` without a live cluster.

## Minimum summary

For each setting, fault case, and property, report each available measure:

- attempted operations;
- successful histories;
- PASS, VIOLATION, and UNAVAILABLE counts;
- violation rate;
- availability rate;
- p50, p95, and p99 latency; and
- recovery time after primary failure/election.

Every table cell names the workload, fault schedule, version range, and repetition count behind it. No cell is a universal guarantee.
