# Report plan

The report is a short experimental systems paper. Each result must be traceable through this chain:

```text
question -> theory -> prediction -> adversarial experiment -> recorded history -> checker -> explanation
```

## Section plan

1. Abstract: state the system, four properties, configuration factors, failure instruments, and the measured outcomes. Do not state a result before the raw campaign and analysis are complete.
2. Introduction: motivate client-visible consistency and state RQ1 and RQ2 separately.
3. Background: define RYW, WFR, MR, and MW from the DSA5208 slides. Explain read concern, write concern, explicit causal sessions, primary election, and the limits of scalar ordering.
4. Deployment: describe the three-member Compose replica set, client and replica networks, versions, image digest, and the restricted runner boundary.
5. Predictions: show C1-C8 and the frozen guarantee targets. State that an unguaranteed cell permits a counterexample but does not predict one on every run.
6. Method: define the logical document, integer versions, explicit dependencies, operation records, property schedules, timeout classes, outcome classes, campaign order, and history hashes.
7. Results: report raw outcome counts, consistency rates, operation success, history completion, latency, election and recovery time, and factorial contrasts.
8. Discussion: explain representative traces through the recorded routing, fault event, dependencies, and checker reason. Keep observed behaviour separate from MongoDB internal mechanisms that were not inspected.
9. Limits: cover laptop-hosted replicas, synthetic network faults, finite schedules, timing sensitivity, version scope, and the boundary between RQ1 and RQ2.
10. Reproduction: give the setup, pilot, serial or isolated-parallel main campaign, analysis, and submission commands. State which commands require Docker and which are offline.
11. Conclusion: answer only the questions supported by the recorded histories.
12. Tool use: disclose code-generation assistance and identify which claims were checked by tests or raw artifacts.

## Required figures

- Compose architecture with separate client and replica paths.
- Four slide-style counterexample timelines, one each for RYW, MR, MW, and WFR.
- Normal topology and each fault topology used by the schedules.
- Configuration-property outcome heatmap.
- Factorial interaction plots for read concern, write concern, and causal session.
- Representative raw trace with requested and actual routing.
- Latency plot with p50, p95, and p99 values.
- Prediction-versus-observation table or figure.

## Required tables

- Software versions, image digest, member addresses, and timeout policy.
- C1-C8 settings and prediction targets.
- Property definitions and operation sequences.
- Counts for `PASS`, `VIOLATION`, `UNAVAILABLE`, `INDETERMINATE`, `HARNESS_ERROR`, and `UNSUPPORTED`.
- Main effects and interactions for each property and metric.
- One explanation table mapping a prediction to a representative history and checker reason.
- Limitations and unanswered questions.

## Evidence rules

- The prediction manifest is committed before result files.
- A result claim must cite a raw history, manifest, derived summary, or a checked source page.
- `PASS` and `VIOLATION` determine the consistency denominator. Other outcomes stay visible and are not silently discarded.
- Keep the 192-history pilot separate from the 1,280-history main campaign. Report the 320 normal controls and 960 adversarial histories in separate columns; RQ1 rates and factorial contrasts use the adversarial histories only.
- A timeout after a possibly completed write is `INDETERMINATE`.
- Absence of a violation in the tested histories is not a universal guarantee.
- A checker result does not establish an internal MongoDB mechanism unless an observable trace supports that explanation.
- Parallel workers are an execution detail. Report their count and resource isolation, and treat latency as measured under that declared level of concurrency.
- If a campaign is resumed across execution modes or runner revisions, report the accepted history count for each mode, keep failed harness attempts outside `results/raw/`, and qualify pooled latency as descriptive under mixed host load.
- State property-specific violation denominators and the sample count behind any unusually high cell rate. Factorial contrasts use equal-weight available cell rates here and are descriptive, not inferential.

## Source links

- [Assignment summary](assignment.md)
- [Project plan](project-plan.md)
- [Slide alignment](slide-alignment.md)
- [Experimental protocol](experimental-protocol.md)
- [Step 1 decisions](step-1-decisions.md)
