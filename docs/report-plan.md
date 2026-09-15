# Report plan

## Title

Experimental Evaluation of Client-Centric Consistency in MongoDB under Replica Failures and Network Partitions

Write each result in this order: question, theory, prediction, experiment, recorded history, checker output, and explanation.

## Sections

1. **Abstract:** question, method, main measurements, and limits of the claims.
2. **Introduction:** motivation, contribution, and research questions.
3. **Background and formal model:** RYW, MR, MW, WFR, causal precedence, logical versions, and MongoDB semantics used in the tests.
4. **Deployment and settings:** architecture, exact software versions, installation steps, and selected settings.
5. **Predictions:** the prediction matrix and the reason for each prediction, written before reading the main results.
6. **Method and fault model:** workloads, history fields, checker predicates, controls, repetitions, and fault schedules.
7. **Results:** counts, rates, latency, recovery, and representative histories.
8. **Discussion:** prediction versus observation, mechanisms, and the consistency, availability, and latency trade-off.
9. **Limits:** container topology, synthetic network faults, timing variation, finite trials, workload scope, and version scope.
10. **Reproduction:** commands, repository layout, and steps for rebuilding tables and figures.
11. **Conclusion:** findings limited to the tested system and open questions.
12. **References and tool-use disclosure:** MongoDB, PyMongo, course material, fault-injection tools, and the required AI-use disclosure.

Put Compose commands, fault scripts, selected raw traces, checker pseudocode, and extra plots in the appendix.

## Figures

1. Test setup: runner, MongoDB replica set, fault controller, history recorder, and checker.
2. One minimal counterexample history for each of RYW, MR, MW, and WFR.
3. The three fault layouts: normal operation, node crash, and network partition.
4. Results by setting, fault case, and consistency property.
5. One real violation trace with logical versions and member identities.
6. Consistency versus availability.
7. Latency distribution with p50, p95, and p99, an ECDF, or a box plot.

Do not use terminal, Docker Desktop, VS Code, or installation screenshots in the main report unless a screenshot proves an observation.

## Tables

1. Properties and executable checker predicates.
2. Software, hardware, and deployment versions.
3. Predictions made before the campaign.
4. Controls and independent variables.
5. Results: successful histories, violations, availability, and latency.
6. Prediction, observation, and explanation.
7. Limits and mitigations.

## Claims and evidence

The report needs to show five things:

1. Predictions were recorded before the results were interpreted.
2. Each reported violation has an executable predicate.
3. The workloads tried to create the predicted counterexamples.
4. A consistency violation is separate from an unavailable or timed-out operation.
5. Another person can run the experiment from the documented commands.

Zero violations in a finite campaign means that no counterexample appeared under the tested workload and fault schedule. It does not establish a universal guarantee.

## Reproduction package

Use this layout as the implementation grows:

```text
results/raw/       raw operation histories
results/summary/   aggregated metrics
figures/           generated plots
scripts/           setup and fault scripts
src/               experiment runner and checkers
tests/             checker unit tests
```

The target commands are:

```bash
make setup
make experiment
make analyse
```

`make analyse` must rebuild the summaries and figures from `results/raw/` without a live cluster. Every trace used in the report must identify its setting, fault schedule, software versions, and checker version.

## Sources

- DSA5208 Lecture 1: physical and logical time, causal precedence, and Lamport clocks.
- DSA5208 Lecture 3: the four client-centric models, MongoDB read/write concern, majority snapshots, and causal sessions.
- MongoDB documentation for causal consistency, read concern, write concern, replica sets, elections, and the server version used.
- PyMongo documentation for sessions and read/write concern APIs.
- Documentation for each fault-injection tool used.
- The AI-use disclosure required by the course.
