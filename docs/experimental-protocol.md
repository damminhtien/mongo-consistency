# Experimental protocol

## Trial unit

A trial is one configuration, property, schedule, seed, and unique namespace. The three-member replica set stays alive for the trial. The runner does not reset the database after each property. A trial records the initial document state `x=v0`; that initialization is outside the property predicate.

Each trial has a manifest with these fields:

| Field | Requirement |
| --- | --- |
| `schema_version` | Version of the raw-history schema. |
| `trial_id` | Globally unique trial identifier. |
| `campaign_id` | `pilot`, `normal`, or `adversarial`. |
| `configuration_id` | One of C1-C8. |
| `read_concern` | `local` or `majority`. |
| `write_concern` | `w:1` or `majority`, including timeout settings. |
| `causal_session` | `true` or `false`; both use an explicit session. |
| `property` | `RYW`, `MR`, `MW`, or `WFR`. |
| `schedule_id` | Registered fault and operation schedule. |
| `seed` | `20260915 + campaign_ordinal`. |
| `software_versions` | Python, PyMongo, Docker, Compose, and MongoDB versions. |
| `image_digest` | Resolved MongoDB image digest. |
| `members` | Member names, addresses, and roles at setup. |
| `timeout_policy` | Connection, operation, election, and subtrial limits. |
| `fault_policy` | Fault events, targets, and cleanup result. |
| `prediction_commit` | Commit containing the frozen prediction manifest. |
| `runner_version` | Runner source revision and schema version. |
| `checker_version` | Offline checker source revision and schema version. |
| `history_hash` | SHA-256 of canonical raw history bytes. |

## Logical history

The workload uses one logical document per trial:

```json
{
  "_id": "trial-id/x",
  "updates": [
    {
      "write_id": "w1",
      "version": 1,
      "effect": "set-v1",
      "parent_write_id": null,
      "depends_on_read_id": null,
      "depends_on_version": null
    }
  ]
}
```

One logical writer allocates integer versions. The checker never treats a Lamport tuple, server timestamp, or wall-clock timestamp as evidence that one application update supersedes another. `parent_write_id`, `depends_on_read_id`, and `depends_on_version` are explicit application dependencies. Server metadata is diagnostic only.

MW and WFR use this same document. A history using `x` for one operation and `y` for the other is rejected as an invalid fixture for either predicate.

The observer reads the complete `x` document once. It records the returned update list, the observed version set, and the read timestamp. It does not combine fields from multiple reads. The versioned record contracts in `schemas/` define the serialized form.

## Recorded operation

Every operation record includes:

```text
operation_id
trial_id
property
kind
requested_member
actual_server_address
actual_role
session_id
causal_session
read_concern
write_concern
start_ns
end_ns
operation_status
error_code
error_message
timeout_category
fault_event_id
dependency_metadata
document_version_before
document_version_after
```

The requested member is the routing intent. The command monitor supplies the actual server address and command name. The runner records the role observed during setup and the command timing. A successful primary write uses the driver-selected primary; tagged secondary reads use a named secondary tag. Application versions before and after an operation are recorded when the schedule makes them observable.

## Property checkers

The checkers consume a canonical history and return one outcome plus a reason. They do not infer hidden database state.

- RYW passes when the read of `x` returns the version written by the preceding write or a later version. A lower observed version is a violation.
- MR passes when the second read returns the version from the first read or a later version. A lower second observation is a violation.
- MW passes when the observer sees a state containing the successor write without the predecessor write only if the schedule defines that state as the tested outcome. The checker marks a completed predecessor and successor on the same `x`; a successor that is visible while the predecessor is absent is a violation.
- WFR passes when a write that explicitly depends on the version read by R1 is observed together with its dependency. A dependent successor on `x` without the version read by R1 is a violation.

The checkers require the expected operation IDs, same-key identity, dependency fields, and a complete observer snapshot. Missing or contradictory records are not converted into a database violation.

## Outcomes

| Outcome | Meaning | Included in database metrics |
| --- | --- | --- |
| `PASS` | The recorded complete history satisfies the property. | Yes. |
| `VIOLATION` | The recorded complete history contradicts the property. | Yes. |
| `UNAVAILABLE` | The required read or write could not be served under the schedule. | No for violation rate; retained separately. |
| `INDETERMINATE` | A required result is ambiguous, such as a lost write response or timeout after a possible mutation. | No for violation rate; retained separately. |
| `HARNESS_ERROR` | The runner, fault controller, or recorder failed independently of the database outcome. | No. |
| `UNSUPPORTED` | A required topology precondition could not be established. | No. |

The consistency violation rate is `VIOLATION / (PASS + VIOLATION)`. A write timeout or lost write response is `INDETERMINATE`, never an assumed failed write.

## Network and fault control

Compose creates two paths:

- `replica_net` carries member-to-member replication and election traffic.
- `client_net` carries runner-to-member traffic.

The fault controller is a separate process with a narrow control interface. It applies named events to replica traffic while the runner keeps client access to the selected member. The runner has no Docker socket, Docker credentials, host filesystem mount, or access to unrelated host data. If a stale-member client path cannot be preserved, the trial is `UNSUPPORTED`.

Every schedule has an event ID, start condition, target members, expected topology state, cleanup action, and cleanup verification. Election and recovery intervals are recorded from the fault events. Cleanup must restore a stable three-member replica set before the next trial; otherwise the history is a harness error.

## Schedules

### RYW

1. Confirm a stable replica set and record `v0`.
2. Isolate replication traffic to the tagged stale secondary while preserving client access to it.
3. Write `v1` on the current primary.
4. Read `x` from the tagged stale secondary in the same explicit session.
5. Record the read result, heal replication, and verify stability.

### MR

1. Confirm a stable replica set and record `v0`.
2. Isolate one tagged secondary while preserving client access.
3. Read `x` from a fresh secondary and record its version.
4. Read `x` from the isolated stale secondary using the same explicit session.
5. Record both observations, heal replication, and verify stability.

### MW

1. Complete W1 on the old primary and record its write ID on `x`.
2. Apply the partition while the workload is ready to issue W2.
3. Wait for a new primary with the 30-second topology barrier.
4. Complete W2 on the new primary, on the same `x`, with its `parent_write_id` set to W1.
5. Run one observer snapshot of `x`.
6. Heal the partition and verify a stable replica set.

The workload is not postponed until after election. W1 is before the partition, and W2 is issued only after the election barrier has completed. The schedule therefore distinguishes predecessor completion from successor visibility.

### WFR

1. Complete R1 on the old side and record the observed version of `x`.
2. Apply the partition while the workload is ready to issue the dependent write.
3. Wait for a new primary with the 30-second topology barrier.
4. Complete W2 on the new primary, on the same `x`, with `depends_on_read_id` and `depends_on_version` set from R1.
5. Run one observer snapshot of `x`.
6. Heal the partition and verify a stable replica set.

### Normal control

The control schedule runs the same operation order and dependency metadata without an injected fault. It uses a fresh namespace and the same configuration cell. It is a baseline for completion, latency, and ordinary operation rather than a substitute for the adversarial schedule.

## Timeouts and campaign order

```text
connectTimeoutMS=2000
serverSelectionTimeoutMS=5000
socketTimeoutMS=5000
operation_deadline=5000ms
wtimeoutMS=5000 where applicable
election_barrier=30000ms
subtrial_deadline=60000ms
retryWrites=false
retryReads=false
```

The campaign uses a seeded, stratified shuffle. The seed is `20260915 + campaign_ordinal`. Each case receives a new trial ID and namespace. The normal baseline contains 320 histories. The adversarial campaign contains 960 histories. Pilot execution uses five adversarial repetitions and one normal smoke trial per configuration/property cell.

## Offline analysis

`make analyse` reads only raw histories, manifests, and the frozen prediction manifest. It recomputes checker outcomes, outcome counts, operation success, history completion, consistency violation rate, p50/p95/p99 latency, election and recovery time, and the read-concern, write-concern, causal-session main effects and interactions. It writes summaries and figures without connecting to MongoDB.
