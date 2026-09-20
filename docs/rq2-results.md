# RQ2 results

RQ2 completed on 20 September 2026 with the locked design: four configurations
(C1/C3/C4/C6), three faults (F1/F2/F3), four properties (RYW/MR/MW/WFR), and
eight repetitions, plus 12 F3 extension episodes that bring four signature
cells to 20 repetitions. The campaign contains 432 histories in 36 fault
episodes. It reuses the RQ1 normal histories as its baseline and adds no normal
histories.

The canonical history files and episode manifest are under
[`results/raw/rq2/`](../results/raw/rq2/). Offline summaries and figures were
rebuilt with `make analyse`; the generated machine-readable summary is
`results/summary/summary.json`. Runner and protocol provenance use commit
`3c35e94` with MongoDB 7.0.34, PyMongo 4.18.1, Docker Engine 29.8.0, and
Docker Compose 5.5.1.
The setup snapshots are preserved in [`toolchain.json`](../results/provenance/rq2-20260920/toolchain.json)
and [`replica-status.json`](../results/provenance/rq2-20260920/replica-status.json).

## Coverage and recovery

| Fault | Episodes | Fault application | Recovery converged | Election success |
| --- | ---: | ---: | ---: | ---: |
| F1 secondary crash | 8 | 8/8 | 8/8 | Not applicable |
| F2 primary crash/election | 8 | 8/8 | 8/8 | 8/8 |
| F3 network partition | 20 | 20/20 | 20/20 | 20/20 |

There were no harness errors, precondition misses, or unavailable histories.
The 432 histories share 36 fault injections; histories within an episode are
separate workloads, not independent fault events.

## Consistency outcomes

| Fault | Property | Histories | PASS | VIOLATION | INDETERMINATE |
| --- | --- | ---: | ---: | ---: | ---: |
| F1 | RYW | 32 | 32 | 0 | 0 |
| F1 | MR | 32 | 32 | 0 | 0 |
| F1 | MW | 32 | 32 | 0 | 0 |
| F1 | WFR | 32 | 32 | 0 | 0 |
| F2 | RYW | 32 | 32 | 0 | 0 |
| F2 | MR | 32 | 32 | 0 | 0 |
| F2 | MW | 32 | 32 | 0 | 0 |
| F2 | WFR | 32 | 32 | 0 | 0 |
| F3 | RYW | 56 | 16 | 20 | 20 |
| F3 | MR | 32 | 32 | 0 | 0 |
| F3 | MW | 56 | 16 | 20 | 20 |
| F3 | WFR | 32 | 32 | 0 | 0 |

Overall, the campaign recorded 352 PASS, 40 VIOLATION, and 40 INDETERMINATE
histories. The analysis rate is 40/(352+40) = 10.2% among resolved histories;
this is descriptive of this fixed campaign matrix and is not an estimate of a
universal violation probability.

The observer checked all 492 acknowledged writes after recovery; 40 were
observed to have rolled back (8.13%).

## Signature cells

| Configuration × F3 × property | Repetitions | Observed result |
| --- | ---: | --- |
| C1 × RYW | 20 | The same stale-read violation occurred in 20/20 histories: acknowledged version 1 was followed by version 0. |
| C1 × MW | 20 | The same write-order violation occurred in 20/20 histories: successor `w2` was visible while predecessor `w1` was absent. |
| C6 × RYW | 20 | INDETERMINATE in 20/20 histories: the majority write to the isolated primary timed out, so the required read was not issued. |
| C6 × MW | 20 | INDETERMINATE in 20/20 histories: the first majority write timed out, so dependent write `w2` was not issued. |

The C6 timeouts are not counted as consistency passes or violations because the
required schedule did not reach its later operation. The outcome counts above
retain those histories explicitly.

## Episode timing

| Fault | Election p50 | Recovery p50 |
| --- | ---: | ---: |
| F1 | Not applicable | 7.52 s (8 episodes) |
| F2 | 10.36 s (8 episodes) | 9.52 s (8 episodes) |
| F3 | 10.80 s (20 episodes) | 5.05 s (20 episodes) |

These quantiles use fault episodes as the timing unit. All F2 and F3 elections
succeeded, and every fault episode converged after recovery.
