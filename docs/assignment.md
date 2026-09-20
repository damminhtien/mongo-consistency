# Assignment summary

The DSA5208 project installs a distributed database and tests client-visible
consistency properties under normal and failure conditions.

## Group

- DAM MINH TIEN - A0355091E
- NGUYEN MINH DUC - Student ID pending
- VU NHAT MINH THU - Student ID pending

## Selected system

MongoDB is the final database choice. The baseline uses three data-bearing
replica-set members in Docker Compose. Target versions are MongoDB 7.0.34,
PyMongo 4.18.1, Python 3.14.7, Docker Engine 29.8.0, and Docker Compose 5.5.1.
Live setup records the actual runtime versions and MongoDB image digest.

## Properties and scope

The project tests read-your-writes (RYW), monotonic reads (MR), monotonic writes
(MW), and writes-follow-reads (WFR). All operations within a trial use one
logical document x where the property requires a shared key. RQ1 varies read
concern, write concern, and causal-session setting. RQ2 asks how node failures,
primary failover, and network partitions affect client-centric consistency,
operation availability, and recovery in the three-member replica set. It
compares secondary crash, primary crash/election, and primary-isolating network
partition under C1, C3, C4, and C6. RQ2 uses RQ1 normal histories as a
descriptive baseline, covers RYW, MR, MW, and WFR, and does not repeat a
normal campaign.

## Submission

The report and reproduction archive are built from source and available raw
histories. A missing or incomplete campaign remains visibly incomplete; predicted
values are not presented as measured results.

## Repository requirements

- Record requested and actual server routing for each subject operation.
- Use explicit sessions for causal ON and causal OFF.
- Disable retry reads and retry writes.
- Keep PASS, VIOLATION, UNAVAILABLE, INDETERMINATE, PRECONDITION_MISS, and
  HARNESS_ERROR distinct.
- Exclude precondition misses and harness errors from database consistency metrics.
- Reject MW and WFR fixtures that use different logical keys.
- Build summaries offline from raw histories.
- Keep generated outputs and local tooling out of canonical source inputs.
