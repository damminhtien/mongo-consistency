# RQ4 analysis layer

RQ4 asks:

> When MongoDB avoids a client-visible consistency violation, how much latency
> and observed definitive completion does the client pay?

The analysis is derived from immutable raw histories. It makes no MongoDB
connections and does not add a workload, topology, or configuration. The
checkout stores RQ1 under `results/raw/experiment` and RQ2 under
`results/raw/rq2`; `results/raw/rq1` is accepted as an alias for RQ1 input.

## Metrics

For each configuration, scenario, and property, the analyzer writes:

- `violation_rate = VIOLATION / (PASS + VIOLATION)`;
- `definitive_completion_rate = (PASS + VIOLATION) / attempted`;
- `indeterminate_rate = INDETERMINATE / attempted`; and
- critical-operation p50 and p95 latency in milliseconds.

`attempted` is the count of PASS, VIOLATION, UNAVAILABLE, and INDETERMINATE
histories. PRECONDITION_MISS and HARNESS_ERROR stay visible but are excluded
from that denominator because the registered subject operation was not a
definitive consistency attempt. Completion is labelled **observed definitive
completion rate**; it is not formal CAP availability.

Latency uses `end_ns - start_ns` for one operation per property:

| Property | Critical operation |
| --- | --- |
| RYW | read after the own write |
| MR | second read |
| MW | successor write W2 |
| WFR | dependent write |

A timeout with valid timestamps remains a latency observation. Its outcome
remains UNAVAILABLE or INDETERMINATE according to the checker and is not
reclassified as a definite failure.

## Scenarios and contrasts

The analyzer labels RQ1 normal controls as `normal` and RQ1 adversarial
schedules as `rq1_fault`. RQ2 F1, F2, and F3 become `secondary_crash`,
`primary_crash`, and `partition`. Each fault row has a normal RQ1 baseline so
`fault_deltas.csv` can report p95 and completion changes.

`contrasts.csv` keeps the one-factor pairs C1/C2, C7/C3, C8/C4, and C5/C6. It
does not rank configurations. The RQ2 partition campaign contains C1, C3, C4,
and C6 only; C5/C6 partition rows are therefore emitted as `MISSING_CELL`.

## Reproduction

```bash
make analyse RQ=rq4
```

This writes:

```text
results/summary/rq4/metrics.csv
results/summary/rq4/contrasts.csv
results/summary/rq4/fault_deltas.csv
figures/rq4_outcomes_partition.pdf
figures/rq4_latency_completion.pdf
figures/rq4_c5_c6_contrast.pdf
```

The primary figures show partition outcome composition, p95 latency against
observed definitive completion, and the C5/C6 one-factor contrast. Claims stay
conditional on the recorded schedules and sample sizes; p99 is intentionally
not used for the small cells. The command also regenerates
`submission/generated-rq4.tex` and copies the three RQ4 PDFs under
`submission/figures/` so `make submission` includes the current report section.
