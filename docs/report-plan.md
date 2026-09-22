# Report plan

The report follows the Project 1 requirements in the order a reader needs to
evaluate the work: database and deployment, consistency settings and
predictions, experiment design and rationale, checked results, interpretation,
limitations, and reproduction. Each aggregate result is generated from the
campaign summary, which is rebuilt from canonical raw histories.
The front matter places a short acknowledgements section after the abstract.

## Report structure

1. Abstract: scope, campaigns, classification rules, and generated outcomes.
2. Introduction: objective, scope, four client-centric properties, and RQ1-RQ3.
3. Database System and Deployment: MongoDB, topology, recorded versions,
   installation, and deployment verification.
4. Consistency Configurations: read/write concerns, causal sessions, C1-C8,
   expected behavior, and registered targets.
5. Experimental Design: checkers, normal controls, secondary and primary
   failures, network partition, property schedules, rationale, repetitions,
   metrics, and outcome classes.
6. Experiments and Results: RYW, MR, MW, and WFR, each with prediction,
   experiment/rationale, results, and explanation; then cross-configuration and
   cross-scenario tables and one RQ3 mechanism subsection with a timeline and
   evidence table.
7. Discussion: predictions versus observations, configuration and fault
   effects, consistency/availability distinctions, and behavior needing
   interpretation.
8. Limitations and Threats to Validity.
9. Reproducibility.
10. Conclusion.
11. References and a separate AI Usage Statement.
12. Appendices A-D: configuration details; installation/reproduction commands;
    additional results; source structure and requirement map.

## Requirement map

| Project requirement | Report location |
| --- | --- |
| Chosen database and deployment architecture | Section 2 |
| Software versions and installation | Sections 2.3-2.5 |
| Consistency settings and relevant parameters | Sections 3.1-3.3 |
| Expected behavior and property predictions | Section 3.4 |
| RYW, MR, MW, and WFR experiments | Sections 4 and 5.1-5.4 |
| Experiment design and rationale | Sections 4 and 5.1-5.4 |
| Normal operation, node failures, and partition | Sections 4.3-4.5 |
| Reported and explained observations | Section 5 |
| Agreement between expectation and observation | Section 6.1 |
| Mechanism explanation from matched, instrumented histories | RQ3 subsection after the RQ2 tables, linked to selected RQ1 anchor histories |
| Limitations | Section 7 |
| Sources and AI-use disclosure | References and AI Usage Statement |
| Code and reproduction instructions | Section 8, Appendix B, and archive |

## Evidence and reporting rules

- The protocol, schedules, prediction manifest, raw histories, checker, and
  summary connect each reported claim to its source. Do not substitute examples or setup snapshots
  for campaign observations.
- Derive counts and metrics from analysis output. Keep PASS, VIOLATION,
  UNAVAILABLE, INDETERMINATE, PRECONDITION_MISS, and HARNESS_ERROR distinct.
- Only PASS and VIOLATION enter the consistency denominator. State the
  numerator and denominator when reporting a rate.
- Keep normal controls separate from adversarial RQ1 histories. Keep smoke and
  pilot histories separate from the main campaigns.
- Report fault timing by fault episode. Histories that share an episode are not
  independent fault injections.
- Treat an unprobed interval as unmeasured availability. F2 has no subject call
  during the election barrier; scheduled-call success is not continuous
  availability.
- A zero observed violation count is finite evidence for the registered
  schedule, not proof of a universal guarantee or a zero true probability.
- Explain internal database behavior only when the recorded trace supports it;
  otherwise describe the observation and mark the mechanism as unobserved.
- For RQ3, distinguish matched seeds from shared fault episodes, and describe a
  timeout as no observed response rather than proof of server-side waiting.
- Report the six registered historical RQ1 anchors separately from the
  matched RQ3 replays. Verify their raw and canonical history hashes. The anchor
  pairs have different seeds; the M3 pair also has different election paths.
- For RQ3 replays, report control validity separately from consistency
  outcomes. Re-derive validity from raw histories; invalid pairs are diagnostic
  and do not enter signature counts. Select the first preregistered valid pair
  for trace display rather than selecting by outcome. Retain anchor, protocol,
  campaign, and topology-plan hashes with the analysis artifacts.
- Read the replay manifest from `results/raw/rq3/campaign-manifest.json` and the
  topology rehearsal from `results/raw/rq3-preflight.json`. Record the
  `rq3-anchor-selection.v1`, `rq3-campaign.v2`, `rq3-preflight.v2`,
  `rq3-analysis.v2`, and `rq3-selection.v2` schema versions. Until the six
  anchors verify and the eight-pair-per-contrast replay passes its controls,
  report RQ3 as pending and make no mechanism-result claims.
- Identify the host operating-system release as not recorded when describing
  campaign provenance. Do not replace historical provenance with the current
  machine's state.
