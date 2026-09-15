# Report and reproducibility plan

## 1. Proposed report

Proposed title:

> Experimental Evaluation of Client-Centric Consistency in MongoDB under Replica Failures and Network Partitions

The report should read as a small experimental-systems paper, not as an installation log. Each result should follow:

```text
Question -> Theory -> Prediction -> Adversarial Experiment
         -> Recorded History -> Checker -> Explanation
```

## 2. Section outline

1. **Abstract** — question, method, main measured trade-offs, and scope of claims.
2. **Introduction** — motivation, contribution, and research questions.
3. **Background and formal model** — RYW, MR, MW, WFR, causal precedence, logical versions, and relevant MongoDB semantics.
4. **MongoDB deployment and configuration** — architecture, software versions, installation procedure, and selected configurations.
5. **Predictions and hypotheses** — predictions written before measurements are interpreted.
6. **Experimental methodology and fault model** — workloads, history schema, checker predicates, controls, repetitions, and faults.
7. **Results** — quantitative summaries, representative traces, and availability/latency effects.
8. **Discussion** — prediction versus observation, mechanisms, and consistency–availability–latency trade-offs.
9. **Threats to validity and limitations** — container topology, synthetic network faults, nondeterminism, finite sampling, workload scope, and version scope.
10. **Reproducibility** — setup, experiment, analysis commands, repository layout, and how to regenerate tables and figures.
11. **Conclusion** — bounded conclusions and unanswered questions.
12. **References and AI usage** — all technical sources and the required AI-use disclosure.

Appendix material should include installation commands, Docker Compose configuration, fault scripts, selected raw traces, checker pseudocode, and additional plots.

## 3. Priority figures

1. Experimental architecture: runner, MongoDB replica set, fault controller, history recorder, and checker.
2. Four minimal counterexample histories for RYW, MR, MW, and WFR.
3. Fault topology for normal operation, node crash, and network partition.
4. Main-result heatmap across configuration × failure × consistency property.
5. One representative real violation trace with exact versions and node identities.
6. Consistency/availability trade-off.
7. Latency distribution, preferably p50/p95/p99, ECDF, or box plot.

Do not use terminal, Docker Desktop, VS Code, or installation screenshots in the main report unless a screenshot directly proves an experimental observation.

## 4. Priority tables

1. Formal consistency properties and executable violation predicates.
2. Exact software, hardware, and deployment versions.
3. Prediction matrix prepared before results.
4. Experimental controls and independent variables.
5. Main quantitative results: violations, successful histories, availability, and latency.
6. Prediction versus observation versus explanation.
7. Threats to validity and mitigations.

## 5. Evidence rules

The project should make five facts obvious to the marker:

1. Predictions were made before measurements were interpreted.
2. Each alleged consistency violation has a formal, executable predicate.
3. Experiments actively try to falsify predictions using adversarial replica states.
4. Consistency violations are distinguished from unavailability and timeouts.
5. The full experiment can be reproduced from a small documented command sequence.

Zero observed violations means only that no counterexample was observed under the tested workload and fault schedule. It does not prove a universal guarantee.

## 6. Reproducibility package

The repository should converge on this layout:

```text
results/raw/       raw operation histories
results/summary/   aggregated metrics
figures/           generated plots
scripts/           setup and fault-injection scripts
src/               experiment runner and checkers
tests/             checker unit tests
```

Target commands:

```bash
make setup
make experiment
make analyse
```

The analysis command must be able to regenerate summaries and plots from the raw histories without requiring a live cluster. Raw traces used in the report must remain linked to their configuration, fault schedule, software versions, and checker version.

## 7. Sources to cite

- DSA5208 Lecture 1: physical and logical times, causal precedence, and Lamport logical clocks.
- DSA5208 Lecture 3: the four client-centric models and MongoDB read/write concern, majority snapshots, and causal-session discussion.
- Official MongoDB documentation for causal consistency, read concern, write concern, replica sets, elections, and the exact server version used.
- PyMongo documentation for session and read/write concern APIs.
- Documentation for any fault-injection tool used.
- AI usage disclosure required by the assignment.
