# Report plan

The report presents the final protocol and its measured evidence. It does not
recount development attempts or copy unverified values into LaTeX. Every result
must be reproducible from this chain:

~~~text
question -> prediction -> registered schedule -> raw history -> offline checker -> analysis
~~~

## Sections

1. Abstract: scope, factors, topology, campaign status, and results generated
   from the final analysis.
2. Introduction: state RQ1 and RQ2 separately and define the client-visible
   consistency problem.
3. Background: explain RYW, MR, MW, WFR, logical versions, and the limits of
   inferring causality from scalar timestamps.
4. Deployment: describe the three-member Compose topology, network paths,
   actual software versions, image digest, and runner boundary.
5. Predictions: show C1-C8 and the durability-aware guarantee targets.
6. Method: define the logical document, explicit session, preconditions,
   property-specific schedules, independent observer, timeouts, outcomes, and
   provenance.
7. Results: derive counts, rates, latency, election/recovery times, contrasts,
   figures, and representative traces from the campaign artifacts.
8. Discussion: compare predictions with observations and explain only what the
   trace supports.
9. Limits: cover one-host containers, synthetic faults, finite schedules, and
   the limits of generalizing to other versions or deployments.
10. Reproduction: distinguish Docker-required commands from offline analysis
    and submission builds.
11. Conclusion: answer only the research questions supported by completed
    histories.
12. Tool-use disclosure and evidence appendix.

## Required tables and figures

- Version and image-digest table.
- C1-C8 settings and prediction table.
- Property definitions and operation schedules.
- Outcome counts for PASS, VIOLATION, UNAVAILABLE, INDETERMINATE,
  PRECONDITION_MISS, and HARNESS_ERROR.
- RQ2 outcome, availability, latency, rollback, election, and recovery summaries
  by fault condition, configuration, and property. Show both history counts
  and fault-episode counts; keep episode timing in the separate generated
  `fault-episodes.csv` table.
- Operation success, history completion, consistency rate, p50/p95/p99 latency,
  election and recovery summaries.
- Configuration-by-property comparisons organized as prediction, observation,
  and trace-supported explanation. Keep RC, WC, and causal-session contrasts
  descriptive in the appendix; do not present them as the main contribution or
  claim statistical significance.
- Four slide-aligned property timelines.
- Architecture and fault-topology diagrams.
- Configuration/property outcome heatmap and prediction-versus-observation
  plot.
- Representative trace with requested route, actual server, session metadata,
  fault state, and direct topology evidence.
- The C6 RYW majority-read timeout as an observed unavailable outcome, with its
  source history and hash, not as a stale-read violation.

## Evidence rules

- Commit prediction and protocol inputs before pilot/main results.
- Keep prediction_commit, protocol_commit, and runner_commit distinct.
- Use raw histories as the only source for summary metrics and generated report
  macros.
- The consistency denominator is PASS + VIOLATION. Report all other outcomes
  separately; never count a precondition miss as a database violation.
- Keep the pilot separate from RQ1. Report the 320 normal controls separately
  from the 960 adversarial histories.
- Use the C1/C3/C4/C6 RQ1 normal histories as the RQ2 baseline; do not rerun or
  pool normal histories into RQ2. Report the 384 RQ2 core histories and 48
  partition-signature extensions separately.
- Do not pool parallel execution into RQ1 latency. RQ1 runs sequentially.
- State the numerator and denominator for every property/configuration rate.
- Treat zero observed violations as a finite observation, not proof of a
  universal guarantee.
- Treat each grouped fault episode as the experimental unit for topology,
  election, and recovery measurements. Histories sharing an episode are not
  independent fault events.
- Explain a MongoDB internal mechanism only when the recorded observable trace
  supports that explanation.
- If there is no complete main manifest, report evidence as unavailable; do not
  insert historical counts or predicted values as measurements.
