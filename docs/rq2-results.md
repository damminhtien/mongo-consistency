# RQ2 results

The corrected RQ2 campaign completed 432 histories in 36 fault episodes: eight
secondary losses (F1), eight primary losses and elections (F2), and twenty
replication partitions (F3). The F3 total includes twelve extension episodes
for four registered signature cells. All 36 episodes converged after recovery;
all 28 elections in F2 and F3 succeeded.

The runner commit is `77ce48d503de0a2cd08364edb7786abb85627598`; the
protocol commit is `05c5b655de0866f5587d36ba8722de3e573b10c5`. The raw
histories and episode records are under
[results/raw/rq2/](../results/raw/rq2/). The tables below use the
RQ2 rows from the offline analysis in
[results/summary/summary.json](../results/summary/summary.json).

## Outcomes

| Fault | Episodes | Histories | Recovered | Elections | PASS | VIOLATION | UNAVAILABLE | INDETERMINATE | PRECONDITION\_MISS | HARNESS\_ERROR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| F1 | 8 | 128 | 8/8 | not applicable | 128 | 0 | 0 | 0 | 0 | 0 |
| F2 | 8 | 128 | 8/8 | 8/8 | 128 | 0 | 0 | 0 | 0 | 0 |
| F3 | 20 | 176 | 20/20 | 20/20 | 62 | 56 | 0 | 42 | 16 | 0 |
| Total | 36 | 432 | 36/36 | 28/28 | 318 | 56 | 0 | 42 | 16 | 0 |

The 56 violations are 15.0% of the 374 decidable histories (PASS plus
VIOLATION). This is a description of this campaign matrix, not an estimate of
a general violation probability. PRECONDITION\_MISS histories did not establish
the registered schedule state and are kept outside that denominator. The 56
violations arose from four repeated F3 cells: C1 RYW (20), C1 MW (20), C1 WFR
(8), and C4 WFR (8). They do not represent 56 distinct fault mechanisms.
The previous run used a `w:1` diagnostic seed before a majority read in C3;
31 additional histories missed their setup precondition. The rerun used a
majority-acknowledged seed for MR and for WFR under F1/F2. F3 WFR still uses
`w:1` to create the isolated-branch read.

| Fault | Property | Histories | PASS | VIOLATION | INDETERMINATE | PRECONDITION\_MISS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| F1 | RYW | 32 | 32 | 0 | 0 | 0 |
| F1 | MR | 32 | 32 | 0 | 0 | 0 |
| F1 | MW | 32 | 32 | 0 | 0 | 0 |
| F1 | WFR | 32 | 32 | 0 | 0 | 0 |
| F2 | RYW | 32 | 32 | 0 | 0 | 0 |
| F2 | MR | 32 | 32 | 0 | 0 | 0 |
| F2 | MW | 32 | 32 | 0 | 0 | 0 |
| F2 | WFR | 32 | 32 | 0 | 0 | 0 |
| F3 | RYW | 56 | 16 | 20 | 20 | 0 |
| F3 | MR | 32 | 32 | 0 | 0 | 0 |
| F3 | MW | 56 | 14 | 20 | 22 | 0 |
| F3 | WFR | 32 | 0 | 16 | 0 | 16 |

## WFR schedule

F1 and F2 use a majority-acknowledged diagnostic setup write of version 1
outside the subject session. R1 must return version 1 before the fault is
applied. After recovery, W2 is issued with a dependency on that version.
For F3, the campaign
partitions the old primary first, writes version 1 on that isolated branch
with `w:1`, verifies version 0 on the majority side, and requires R1
to read version 1 before the majority side elects a new primary. The checker
compares R1's returned version with the version in W2's atomic pre-image.

| Fault | Histories | PASS | VIOLATION | PRECONDITION\_MISS |
| --- | ---: | ---: | ---: | ---: |
| F1 | 32 | 32 | 0 | 0 |
| F2 | 32 | 32 | 0 | 0 |
| F3 | 32 | 0 | 16 | 16 |

All F3 WFR violations occurred in C1 and C4, eight histories per
configuration. Each had a successful R1 at version 1 followed by a completed
W2 whose pre-image was version 0. For example,
`rq2-00036-C1-wfr` records R1 at version 1 on the isolated primary and
a successful W2 on the elected primary with
`write_base_version = 0`. C3 and C6 each had eight
PRECONDITION\_MISS histories because the required read did not return version
1 on the isolated branch. Those records do not count as WFR passes.

The F3 setup W1 uses `w:1` for every configuration and runs outside the subject
session. In C4, R1 uses `local` read concern and can return this version from the
isolated old primary; the subject W2 uses C4's `majority` write concern on the
new primary. The C4 WFR violations therefore do not show a failure of a
majority-acknowledged setup write or contradict C4's targeted MW prediction.

## Operations and recovery

| Fault | Successful / attempted subject operations | Success rate | All-attempt p50 / p95 (ms) | Successful-only p95 (ms) | Acknowledged writes / rolled back |
| --- | ---: | ---: | ---: | ---: | ---: |
| F1 | 256 / 256 | 100.0% | 1.33 / 7.44 | 7.44 | 128 / 0 |
| F2 | 256 / 256 | 100.0% | 1.36 / 8.45 | 8.45 | 128 / 0 |
| F3 | 254 / 296 | 85.8% | 1.71 / 4994.19 | 8.26 | 122 / 40 |
| Total | 766 / 808 | 94.8% | 1.46 / 4984.64 | 8.00 | 378 / 40 |

All-attempt latency retains operations with valid start and end times,
including timeouts. The F3 p95 is therefore close to the five-second client
timeout. Successful-only latency excludes those timed-out operations. The
independent observer checked all 378 acknowledged writes after recovery and
found 40 absent from the converged histories; all 40 were in F3.

F1 and F2 produced no consistency violations in the scheduled calls. The
protocol places post-failover calls after the election barrier, so F2 does not
measure request success during the election interval. F3 produced the
counterexamples and unresolved operations shown above. Histories sharing one
episode also share its topology event; they are not independent fault
injections.
