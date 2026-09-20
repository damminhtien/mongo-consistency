# Slide alignment

The experiment uses the local DSA5208 slides as its teaching reference. The
source PDFs remain outside the repository at
/Users/macbook/Desktop/NUS/DSA5208/.

| Source | Pages | Used for |
| --- | --- | --- |
| Lec0.pdf | 16 | Distributed database project and consistency-testing scope. |
| Lec1.pdf | 17-20 | Logical clocks, Lamport scalar order, causality, and the limits of scalar ordering. |
| Lec3.pdf | 37-38 | Read-your-writes definition and history. |
| Lec3.pdf | 39-40 | Writes-follow-reads definition and history. |
| Lec3.pdf | 41-42 | Monotonic-reads definition and history. |
| Lec3.pdf | 43-44 | Monotonic-writes definition and history. |
| Lec3.pdf | 47-48 | MongoDB primary, secondaries, replica set, and election. |
| Lec3.pdf | 49-51 | Read concern, write concern, majority behaviour, and snapshots. |
| Lec3.pdf | 52-53 | Causal-session limits and the limits of a read/write-concern pair alone. |

## Method changes reflected in the final protocol

- Keep configuration semantics (RQ1) separate from topology-failure comparison
  (RQ2).
- Give every property its own operation order and fault schedule.
- Isolate the stale member before MR creates version 1; verify the fresh/stale
  version split before either subject read.
- For RYW, prove stale-member reachability and state before W1, then prove the
  stale/fresh split before the subject read.
- For MW, isolate the old primary before W1, preserve client access, verify
  W1's actual route, elect a new primary, and issue dependent W2 in the same
  subject session.
- For WFR, create the old-branch version with setup w:1 outside the subject
  session, read a concrete version through the subject session, then issue W2
  only after the majority-side election.
- Observe final MW/WFR state from independent direct clients after healing and
  stable topology; do not use the subject read concern for the checker snapshot.
- Use application versions and explicit dependencies rather than Lamport values
  as proof of application order.
- Keep causal ON and OFF explicit sessions and disable retry reads/writes.
- Classify a lost possible write as INDETERMINATE and a missing schedule state
  as PRECONDITION_MISS.
- Keep operation deadlines separate from election/topology barriers.
- Treat 30 repetitions as registered-history repetitions, not a universal
  probability estimate.
- Use the seeded ordinal rule and unique namespaces; do not reset after each
  property.

## Evidence boundary

The slides motivate the client-visible properties and MongoDB controls. The
runner records observable routing, operation results, dependencies, fault
events, and timing. It does not claim to inspect MongoDB's internal Lamport
counter or prove an internal mechanism from client traces alone.
