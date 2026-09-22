# RQ2 results

> Status: previous development run, not final submission evidence. The records
> below predate the corrected RQ2 WFR schedule and the current write pre-image
> checker contract. Keep them for traceability only; rerun RQ2 before citing
> any count, rate, latency, or mechanism conclusion.

The previous RQ2 run completed on 20 September 2026 with the then-locked design:
four configurations
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

F3 isolated each member from replication traffic in multiple episodes while
leaving its client route available. The table counts subject operations whose
recorded start time was at or after fault application and whose actual server
address was the isolated member.

| Isolated member | Episodes | Routed operations | SUCCESS | INDETERMINATE |
| --- | ---: | ---: | ---: | ---: |
| mongo1 | 8 | 48 | 32 | 16 |
| mongo2 | 5 | 28 | 18 | 10 |
| mongo3 | 7 | 36 | 22 | 14 |

Every isolated member served successful client operations during the fault.
Indeterminate operations are retained as timeouts and do not imply loss of the
client route.

## Scheduled operation outcomes, latency, and rollback

| Fault | Histories | Successful/attempted operations | Scheduled operation success rate | All-attempt latency p50/p95/p99 (ms) | Acknowledged writes checked/rolled back |
| --- | ---: | ---: | ---: | ---: | ---: |
| F1 | 128 | 288/288 | 100.0% | 1.28 / 7.38 / 108.22 | 160/0 |
| F2 | 128 | 288/288 | 100.0% | 1.37 / 6.65 / 8.28 | 160/0 |
| F3 | 176 | 304/344 | 88.4% | 1.36 / 4993.57 / 4996.67 | 172/40 |

The success rate counts completed scheduled subject operations, not continuous
service availability during intervals with no request. For F2, all scheduled
post-election operations completed successfully; the failover nevertheless
introduced a median election interval of 10.36 s during which the experiment
deliberately issued no second subject operation. The 288/288 F2 result therefore
does not measure request success during that election interval. F2 operation
latency quantiles also cover scheduled calls only and exclude the election wait.

All-attempt latency quantiles use every subject operation with recorded start
and end times, including timed-out operations. For F3, the all-attempt p95 is
4,993.57 ms because the 40 INDETERMINATE timeouts are retained. Among the 304
successful F3 operations, p95 latency is 6.97 ms; this conditional quantile
excludes those timeouts. Overall, 880/920 scheduled operations succeeded
(95.7%). All 432 histories reached a classified outcome. The independent
observer checked all 492 acknowledged writes; 40 later rolled back (8.13%).
That rollback rate describes this fixed campaign and is not a real-world
probability estimate. `make analyse` generates
`results/summary/summary.csv` with the 48 fault/configuration/property groups,
including per-cell outcomes, operation denominators, both all-attempt and
successful-only latency, rollback counts, and the matching RQ1 normal baseline.

## Consistency outcomes

RYW and MR use direct client-visible version comparisons. MW uses the
preceding write ID in the successive write's atomic pre-image, and WFR uses
the application version in that pre-image. Post-recovery observations are
separate durability evidence.

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
| C1 × MW | 20 | The direct MW result checks whether W1's write ID is present in W2's atomic pre-image; an acknowledged write absent after recovery is reported separately as rollback evidence. |
| C6 × RYW | 20 | INDETERMINATE in 20/20 histories: the majority write to the isolated primary timed out, so the required read was not issued. |
| C6 × MW | 20 | INDETERMINATE in 20/20 histories: the first majority write timed out, so dependent write `w2` was not issued. |

The C6 timeouts are not counted as consistency passes or violations because the
required schedule did not reach its later operation. The outcome counts above
retain those histories explicitly.

The other 44 core configuration/fault/property cells passed all eight
repetitions. The four signature cells above received 12 additional
repetitions each, for 20 histories per cell.

## Episode timing

| Fault | Election p50 | Recovery p50 |
| --- | ---: | ---: |
| F1 | Not applicable | 7.52 s (8 episodes) |
| F2 | 10.36 s (8 episodes) | 9.52 s (8 episodes) |
| F3 | 10.80 s (20 episodes) | 5.05 s (20 episodes) |

These quantiles use fault episodes as the timing unit. All F2 and F3 elections
succeeded, and every fault episode converged after recovery.
