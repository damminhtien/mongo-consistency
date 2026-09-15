# Slide alignment

The experiment uses the local DSA5208 slides as the teaching reference. The source PDFs remain outside the project tree at `/Users/macbook/Desktop/NUS/DSA5208/`.

| Source | Pages | Used for |
| --- | --- | --- |
| `Lec0.pdf` | 16 | Distributed database project and consistency testing scope. |
| `Lec1.pdf` | 17-20 | Logical clocks, Lamport scalar order, causality, and the limit of scalar ordering. |
| `Lec3.pdf` | 37-38 | Read-your-writes definition and history. |
| `Lec3.pdf` | 39-40 | Writes-follow-reads definition and history. |
| `Lec3.pdf` | 41-42 | Monotonic reads definition and history. |
| `Lec3.pdf` | 43-44 | Monotonic writes definition and history. |
| `Lec3.pdf` | 47-48 | MongoDB primary, secondaries, replica set, and election. |
| `Lec3.pdf` | 49-51 | Read concern, write concern, majority behaviour, and snapshots. |
| `Lec3.pdf` | 52-53 | Causal-session limits and why a read concern/write concern pair is not a complete causal guarantee by itself. |

## Review changes reflected in the implementation plan

- Run each property after its own registered fault schedule rather than running all workloads after one election.
- Use the same logical document `x` for MW and WFR.
- Use integer application versions and explicit dependencies instead of inferring recency from Lamport tuples.
- Add `INDETERMINATE` for a possible mutation whose response was lost or timed out.
- Use an explicit causal-off session instead of omitting the session.
- Register property-specific fault schedules.
- Separate the five-second operation deadline from the 30-second election and topology barrier.
- Treat 30 repetitions as repeated registered histories, not a universal probability estimate.
- Use seeded stratified random order and unique namespaces.
- Do not reset the database after every property.
- Analyse C1-C8 as a three-factor design with main effects and interactions.

## Evidence boundary

The slides motivate the client-visible properties and the MongoDB controls. The runner records observable routing, operation results, dependencies, fault events, and timing. It does not claim to inspect a MongoDB internal Lamport counter or to prove an internal mechanism from a client trace.
