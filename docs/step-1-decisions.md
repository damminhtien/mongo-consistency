# Step 1 decisions

These decisions are closed unless a reproducibility or safety check shows that a recorded assumption cannot be implemented.

## Group

- DAM MINH TIEN - A0355091E
- NGUYEN MINH DUC - Student ID pending
- VU NHAT MINH THU - Student ID pending

## System and toolchain

- Final database: MongoDB replica set.
- Baseline deployment: three data-bearing MongoDB members in Docker Compose.
- Target versions: MongoDB 8.0.32, PyMongo 4.18.1, Python 3.14.7, Docker Engine 29.7.2, and Docker Compose 5.5.0.
- Setup records the actual executable versions and resolved MongoDB image digest.

## Experiment

- Use all eight C1-C8 combinations of read concern, write concern, and causal session.
- RQ1 studies configuration semantics with fault schedules as instruments for exposing stale and causal states.
- RQ2 separately compares topology failures and normal operation.
- Normal baseline: 320 histories.
- Adversarial campaign: 960 histories.
- Main total: 1,280 histories.
- Pilot: five adversarial repetitions and one normal smoke trial per configuration/property cell.
- Seed rule: `20260915 + campaign_ordinal`.
- Use a unique namespace for every trial. Do not reset after every property.
- Shuffle cases with a seeded, stratified order.

## Protocol

- Use one logical document `x` with append-only updates.
- Allocate integer application versions from one logical writer.
- Record `parent_write_id`, `depends_on_read_id`, and `depends_on_version` explicitly.
- Use explicit PyMongo sessions for both causal ON and causal OFF.
- Set `retryWrites=false` and `retryReads=false`.
- Capture requested member, actual server address, actual role, session ID, fault event ID, timing, error details, and history hash.
- Use a five-second operation deadline and a 30-second election/topology barrier.
- Use property-specific RYW, MR, MW, WFR, and normal schedules.

## Outcomes and analysis

- History outcomes are `PASS`, `VIOLATION`, `UNAVAILABLE`, and `INDETERMINATE`.
- `HARNESS_ERROR` and `UNSUPPORTED` are retained but excluded from database metrics.
- A lost write response or write timeout is `INDETERMINATE` because the write may have completed.
- Report operation success, history completion, `VIOLATION / (PASS + VIOLATION)`, latency quantiles, election and recovery time, and factorial main effects and interactions.
- Commit predictions before result files.
- Rebuild summaries and figures offline from raw histories.

## Verification before the main campaign

The main campaign is blocked until checker fixtures, malformed-history handling, same-key MW/WFR checks, actual routing capture, stale-member access, independent election barriers, fault cleanup, and runner isolation have passed.
