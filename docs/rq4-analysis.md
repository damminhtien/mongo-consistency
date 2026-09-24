# RQ4 analysis layer

RQ4 asks:

> When a configuration does not expose a client-centric consistency violation
> under a fault, what client-visible outcome occurs instead: successful
> completion, waiting or timeout, an ambiguous outcome, or increased response
> time?

The analysis is derived from immutable raw histories. It makes no MongoDB
connections and does not add a workload, topology, or configuration. The
checkout stores RQ1 under `results/raw/experiment` and RQ2 under
`results/raw/rq2`; `results/raw/rq1` is accepted as an alias for RQ1 input.

## Metrics

For each configuration, scenario, and property, the analyzer writes:

- `violation_rate = VIOLATION / (PASS + VIOLATION)`;
- `decidable_history_rate = (PASS + VIOLATION) / valid_trials`;
- `operation_completion_rate = critical_operation_completed_count / critical_operation_issued_count`;
- `indeterminate_rate = INDETERMINATE / valid_trials`; and
- all-attempt critical-operation p50 and p95 latency in milliseconds; and
- resolved critical-operation p50 and p95 latency for PASS and VIOLATION
  histories only.

`valid_trials` counts PASS, VIOLATION, UNAVAILABLE, and INDETERMINATE
histories. PRECONDITION_MISS and HARNESS_ERROR stay visible but are excluded
from this denominator. The **decidable-history rate** describes whether a
valid history produced a consistency outcome. The **operation completion
rate** describes whether the registered critical operation returned a
response: an issued operation is one with a record in the history, and a
completed operation has `response_received = true`, including a received
server-error response. Its denominator is the number of issued
critical-operation records; the rate is undefined when that count is zero. A
response may be received even when the history is INDETERMINATE because
required checker evidence is missing.

For MW, PASS and VIOLATION use the preceding write ID in the successive
write's atomic pre-image. For WFR, they use the application version in that
pre-image. They therefore test the Lecture 3 conditions directly when the
required evidence is present. Post-recovery observations remain separate
durability evidence.

Latency uses `end_ns - start_ns` for one operation per property:

| Property | Critical operation |
| --- | --- |
| RYW | read after the own write |
| MR | second read |
| MW | successor write W2 |
| WFR | dependent write |

A timeout with valid timestamps remains in the all-attempt latency sample. Its
outcome remains UNAVAILABLE or INDETERMINATE according to the checker and is
not reclassified as a consistency violation. The resolved sample is limited to
PASS and VIOLATION histories.

## Scenarios and contrasts

The analyzer labels RQ1 normal controls as `normal` and RQ1 adversarial
schedules as `rq1_fault`. RQ2 F1, F2, and F3 become `secondary_crash`,
`primary_crash`, and `partition`. Each fault row has a normal RQ1 baseline so
`fault_deltas.csv` can report p95 and completion changes.

`contrasts.csv` keeps the one-factor pairs C1/C2, C7/C3, C8/C4, and C5/C6. It
does not rank configurations. Its status distinguishes missing cells,
complete p95 comparisons, partial latency, and pairs with no latency data. The
RQ2 partition figure and table use the balanced C1/C6 RYW and MW signature
cells; MR and WFR are not pooled into those comparisons.

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
figures/rq4_decidability_latency.pdf
figures/rq4_c5_c6_contrast.pdf
```

The primary figures show paired partition signature outcomes, decidable-history
rate alongside all-attempt p95 latency, and the C5/C6 one-factor contrast.
Claims stay conditional on the recorded schedules and
sample sizes; p99 is intentionally not used for the small cells. The command
writes only machine-readable summaries and figures under `results/summary/rq4/`
and `figures/`; it does not create a document source or modify the code
submission package.
