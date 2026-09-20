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
- Operation success, history completion, consistency rate, p50/p95/p99 latency,
  election and recovery summaries.
- RC, WC, causal-session contrasts and interactions where the data support
  them; label these exploratory and descriptive.
- Four slide-aligned property timelines.
- Architecture and fault-topology diagrams.
- Configuration/property outcome heatmap and prediction-versus-observation
  plot.
- Representative trace with requested route, actual server, session metadata,
  fault state, and direct topology evidence.

## Evidence rules

- Commit prediction and protocol inputs before pilot/main results.
- Keep prediction_commit, protocol_commit, and runner_commit distinct.
- Use raw histories as the only source for summary metrics and generated report
  macros.
- The consistency denominator is PASS + VIOLATION. Report all other outcomes
  separately; never count a precondition miss as a database violation.
- Keep the pilot separate from RQ1. Report the 320 normal controls separately
  from the 960 adversarial histories.
- Do not pool parallel execution into RQ1 latency. RQ1 runs sequentially.
- State the numerator and denominator for every property/configuration rate.
- Treat zero observed violations as a finite observation, not proof of a
  universal guarantee.
- Explain a MongoDB internal mechanism only when the recorded observable trace
  supports that explanation.
- If there is no complete main manifest, report evidence as unavailable; do not
  insert historical counts or predicted values as measurements.
