# Experimental protocol

The protocol is the scientific source of truth for the experiment. The runner,
schemas, analysis, report, and reproduction commands must agree with it. Any
change to a frozen prediction or schedule requires a new protocol commit before
the associated campaign starts.

## Research scope

RQ1 is:

> How do read concern (RC), write concern (WC), and causal sessions affect
> read-your-writes (RYW), monotonic reads (MR), monotonic writes (MW), and
> writes-follow-reads (WFR)?

The deployment has three data-bearing MongoDB members in Docker Compose. RQ1
compares configuration semantics. Property-specific stale-replica and election
schedules are instruments for constructing the states needed to test each
property; they are not a second, combined topology treatment. RQ2 asks:

> How do node failures, primary failover, and network partitions affect
> client-centric consistency, operation availability, and recovery in a
> three-member MongoDB replica set?

The configuration matrix is:

| ID | Read concern | Write concern | Causal session |
| --- | --- | --- | --- |
| C1 | `local` | `w:1` | off |
| C2 | `local` | `w:1` | on |
| C3 | `majority` | `w:1` | on |
| C4 | `local` | `majority` | on |
| C5 | `majority` | `majority` | off |
| C6 | `majority` | `majority` | on |
| C7 | `majority` | `w:1` | off |
| C8 | `local` | `majority` | off |

The prediction manifest uses a durability-aware interpretation of the
documented guarantees. C3 targets MR and WFR; C4 targets MW; C6 targets all four
properties. Other cells are not assigned a guarantee target. A target is a
prediction about successful histories under the registered schedule, not a
claim that every repetition must pass. The eight configurations form a
three-factor matrix. Factorial contrasts are exploratory descriptions of the
observed cells, not a complete inferential factorial experiment.

## Actors and topology

Each trial has three distinct actors:

1. The subject client issues the tested operations through exactly one explicit
   PyMongo `ClientSession`. Causal ON sets `causal_consistency=True`; causal OFF
   sets it to `False`. Neither condition omits the session.
2. The diagnostic layer uses independent direct connections to `mongo1`,
   `mongo2`, and `mongo3`. It observes topology and document state but never
   uses or advances the subject session.
3. The fault controller changes replica-network reachability only. The runner
   has no Docker socket, Docker credentials, or unrelated host-data mount.

`TopologyOracle` sends `{hello: 1}` directly to each member. It does not infer
roles from the subject driver's topology cache and has no guessed-secondary
fallback. A stable topology means exactly one reachable `PRIMARY` and two
reachable `SECONDARY` members in replica set `rs0`. The oracle also records
election ID, set version, member write optime, and the primary's last committed
optime when available. Driver-cached roles are retained only as separate
diagnostic fields.

The Compose network has a client path and a replica path. A partition blocks
replication traffic on the replica path while keeping runner access to the
selected member. Isolation is verified by controller state, direct member
access, and the expected document versions. A successful firewall command by
itself is not proof that the intended state exists.

## Trial unit and logical history

A trial is one configuration, property, schedule, seed, and unique namespace.
The same three-member replica set remains alive; the runner does not reset the
database after each property. Each trial creates a separate logical document
`x` and first verifies that all members contain version 0 (`init`).

The document stores an append-only update list. One logical writer allocates
integer versions. Each update records `write_id`, `version`, `effect`,
`parent_write_id`, `depends_on_read_id`, and `depends_on_version`. These fields
express application-level dependencies; server timestamps and cluster metadata
are diagnostic evidence, not substitutes for those dependencies. MW and WFR
use the same document. A different-key history is invalid for either checker.

Every history stores a first-class precondition object:

```json
{
  "status": "SATISFIED",
  "checks": [{"name": "all-members-at-v0", "status": "SATISFIED"}]
}
```

Its status is `SATISFIED` or `PRECONDITION_MISS`. Each check records the
expected state, actual observation, and observation time. Subject operations
are not issued after a required precondition fails.

## Operation and diagnostic evidence

For each subject operation the recorder stores operation and session IDs,
requested member, command-monitor `actual_server_address`, directly observed
`actual_role` and observation time, separate driver-reported role, causal-session
setting, read/write concern, fault event, start/end times, status, error details,
logical dependencies, and observed document contents. It records session
`$clusterTime` and `operationTime` before and after the call, plus command
`$clusterTime`, `operationTime`, read/write concern, and `afterClusterTime` when
present. `observed_*` fields come from database responses or direct diagnostic
reads, never from version arithmetic.

The command monitor records whether a command reached a server. A network or
server-selection error before a write was sent is `UNAVAILABLE`; a timeout or
lost response after a write may have reached MongoDB is `INDETERMINATE`.
Definitive server responses remain distinguishable from harness failures.

## Registered schedules

Each property has its own operation order. No shared `partition -> election ->
run all properties` schedule is used. `SUBJECT` marks operations issued through
the tested session; setup and diagnostic operations are outside the tested
history.

### RYW: stale-secondary read after write

1. Verify stable topology and version 0 on all members.
2. Select stale secondary `S_stale` and fresh secondary `S_fresh`.
3. Isolate `S_stale`'s replication path while preserving client access.
4. Verify `S_stale` is reachable, still a secondary, and contains only `v0`.
5. `SUBJECT W1(x,v1)` on the primary; record its actual server.
6. Verify `S_fresh` contains `v1` and `S_stale` still contains `v0`.
7. `SUBJECT R1(x)` routed to `S_stale` in the same session.
8. Heal, verify stable topology, then collect independent post-heal observations.

The diagnostic split `V(S_fresh)=1` and `V(S_stale)=0` is required before R1.
If R1 returns a version lower than W1, the history is a violation. If R1 cannot
complete, the result is unavailable rather than a consistency violation.

### MR: fresh read followed by stale read

1. Verify stable topology and version 0 on all members.
2. Select `S_stale` and `S_fresh`; isolate `S_stale` before creating version 1.
3. Verify `S_stale` remains directly reachable as a secondary at `v0`.
4. `SETUP W1(x,v1)` on the healthy primary with majority acknowledgement. This
   setup write is not a subject operation and does not use the subject session.
5. Verify `S_fresh` contains `v1` and `S_stale` contains `v0`.
6. `SUBJECT R1(x)` from `S_fresh`, followed by `SUBJECT R2(x)` from
   `S_stale`, using the same explicit session.
7. Heal, verify stable topology, then collect independent post-heal observations.

The diagnostic split is required before R1. If R2 returns a lower version than
R1, the history is an MR violation.

### MW: ordered writes across an election

1. Verify stable topology and version 0 on all members; record old primary
   `P_old`.
2. Isolate `P_old` from replica peers while preserving client access.
3. Directly verify that `P_old` remains reachable and reports writable primary.
4. Immediately issue `SUBJECT W1(x,v1)` through the same session and verify its
   actual server is `P_old`.
5. Wait independently for the connected majority side to elect `P_new`.
6. Issue `SUBJECT W2(x,v2)` through the same session, with
   `parent_write_id=W1`; verify the actual server is `P_new`.
7. Heal and wait for one primary plus two secondaries.
8. The independent observer reads the complete document directly from all
   three members and waits until all three update lists agree.

If the converged snapshot contains W2 but not W1, the history is an MW
violation. A majority-acknowledged W1 that cannot complete on the isolated old
primary is an availability outcome, not a harness error. An unresolved W2
response is indeterminate.

### WFR: dependent write after a read from the old branch

1. Verify stable topology and version 0 on all members; record old primary
   `P_old`.
2. Isolate `P_old` from replica peers while preserving client access.
3. `SETUP W1(x,v1)` directly on `P_old` with `w:1`; do not use the subject
   session.
4. Verify `P_old` contains `v1` while both majority-side members contain `v0`.
5. Issue `SUBJECT R1(x)` targeting `P_old`; record the concrete version returned.
6. Only after a concrete read version is recorded, wait for the majority side
   to elect `P_new`.
7. Issue `SUBJECT W2(x,v2)` on `P_new` using the same session, with
   `depends_on_read_id=R1` and `depends_on_version` set to R1's returned version.
8. Heal and use the independent, post-heal three-member convergence observer.

If R1 fails, record availability. If R1 returns no concrete version, do not
issue W2 as though the dependency existed. If the converged snapshot contains
W2 but not the version returned by R1, the history is a WFR violation. If the
causal session prevents W2 from completing, retain that outcome rather than
manufacturing a violation.

### Normal control

The control executes the corresponding property operations on a stable,
unpartitioned replica set, with the same configuration, session rules, logical
document, and operation deadlines. It is reported separately from the
adversarial RQ1 estimate.

## Final observation and outcomes

After fault cleanup, the topology oracle must first verify stable topology.
Then an independent diagnostic observer reads the same document directly from
`mongo1`, `mongo2`, and `mongo3`. Convergence requires all three members to be
reachable, return a valid document, and agree on the complete update list. If
topology or document convergence fails, the checker returns `INDETERMINATE`.
The observer never uses the subject session or its read concern.

The six history outcomes are:

| Outcome | Meaning |
| --- | --- |
| `PASS` | A complete, valid history satisfies its predicate. |
| `VIOLATION` | A complete, valid history contradicts its predicate. |
| `UNAVAILABLE` | A required database operation did not complete. |
| `INDETERMINATE` | A possible mutation or required observation has unresolved effect. |
| `PRECONDITION_MISS` | The schedule did not establish the state required by its predicate. |
| `HARNESS_ERROR` | The recorder, runner, controller, or cleanup failed independently of database behavior. |

Only `PASS` and `VIOLATION` enter the consistency denominator:

```text
violation_rate = VIOLATION / (PASS + VIOLATION)
```

Other outcomes remain visible in separate counts. `PRECONDITION_MISS` is not a
database failure and is excluded from consistency metrics.

## Fault and timeout policy

The fault controller verifies its active isolation state and direct client
access. After every fault it attempts healing and verifies stable topology
before another trial starts. Cleanup failure makes the history a harness error
and prevents further use of that unstable cluster.

```text
connect timeout                 2 seconds
server-selection timeout       5 seconds
operation/socket deadline       5 seconds
write-concern timeout           5 seconds
election/topology barrier      30 seconds
subtrial deadline              60 seconds
retryReads=false
retryWrites=false
```

The runner cannot access a Docker socket, Docker credentials, or unrelated host
files. Fault-controller capabilities are isolated to network administration
inside the corresponding MongoDB container's replica interface.

## Campaigns

### Smoke

Run 32 adversarial histories: one for each of eight configurations and four
properties. Smoke checks schedule construction and is never analysis or report
evidence. The gate fails if any property has a precondition-miss rate above 5%,
if any harness error occurs, or if any planned history is missing. The expected
eight trials per property make even one precondition miss a gate failure.

### Pilot

Run five adversarial repetitions per configuration/property cell and one normal
control per cell:

```text
8 x 4 x 5 + 8 x 4 x 1 = 192 histories
```

The pilot validates the frozen harness and estimates schedule stability. Pilot
histories are retained separately and excluded from RQ1 estimates. Any harness
error, routing mismatch, cleanup error, or property precondition-miss rate above
5% blocks the main campaign.

### RQ1 main campaign

Run sequentially on the laptop. Use 10 normal controls and 30 adversarial
histories per configuration/property cell:

```text
normal controls: 8 x 4 x 10 = 320
adversarial:     8 x 4 x 30 = 960
total:                         1280 histories
```

Each namespace is unique. The database is not reset between histories. Use the
fixed seed rule `20260915 + campaign_ordinal` and the deterministic shuffled
plan. Campaign manifests and histories are written atomically. A graceful
shutdown finishes the active trial's cleanup and marks the manifest
`INTERRUPTED`; resume validates case identity, seed, history hash, and frozen
provenance before skipping a completed case. RQ1 latency is not mixed with
parallel execution measurements.

#### Recorded RQ1 result

The canonical RQ1 manifest is `COMPLETE` at 1,280/1,280 histories: 320 normal
controls and 960 adversarial histories. The recorded outcomes are 415 PASS,
443 VIOLATION, 224 UNAVAILABLE, 175 INDETERMINATE, 23 PRECONDITION_MISS, and no
HARNESS_ERROR. Every precondition miss is an adversarial MR history. The runner
records a clean frozen commit (`cc702ab`). Among the 960 adversarial histories,
538 were resolved: 96 PASS and 442 VIOLATION. The consistency violation rate
is 442/538 (82.2%); it describes only resolved histories under these schedules.
The 23 MR PRECONDITION_MISS and 224 UNAVAILABLE histories remain separate
outcomes.

The prediction and protocol inputs are pinned to `b79b567` and `c7e3cf9`.
Raw-record and schema validation passed. The offline summary, per-configuration
prediction/outcome matrix, factorial contrasts, figures, and submission report
were rebuilt from the canonical histories. Smoke manifests are separate
machinery diagnostics and are excluded from RQ1 analysis and counts.

### RQ2 failure comparison

RQ2 keeps the three-member replica set and crosses four sentinel
configurations with three fault conditions and all four client-centric
properties. It does not run another normal condition. The RQ1 normal histories
for C1, C3, C4, and C6 are the descriptive baseline because they use the same
topology and property workload contract. Keep those records in the RQ1
campaign; do not pool them into RQ2.

| Configuration | Read concern | Write concern | Causal session | Role in RQ2 |
| --- | --- | --- | --- | --- |
| C1 | `local` | `w:1` | off | weak baseline |
| C3 | `majority` | `w:1` | on | strong read, weak write durability |
| C4 | `local` | `majority` | on | weak read, strong write |
| C6 | `majority` | `majority` | on | strongest durable causal case |

C5 remains outside RQ2. Comparing C5 with C6 is reserved for RQ3 to isolate
the causal-session setting while holding majority read and write concerns.

The registered topology conditions are:

| ID | Condition | Expected topology |
| --- | --- | --- |
| F1 | Secondary crash | One secondary is stopped; the primary and other secondary remain available. |
| F2 | Primary crash and election | The primary is stopped; the two remaining members elect a new primary. |
| F3 | Primary-isolating network partition | The former primary is separated from the two-member majority side; the client path remains reachable. |

Each core repetition is a grouped fault episode. For each of the three fault
conditions, repetitions 1-8 each contain one history for every C1/C3/C4/C6 and
RYW/MR/MW/WFR combination. Histories keep unique document keys and explicit
client sessions. The fault is applied once at the episode barrier; the runner
records the before-fault operations, applies the fault, runs registered
during-fault operations, completes the post-failover operations, then heals or
restarts the affected member and verifies convergence. This is 3 x 8 x 4 x 4 =
384 histories in 24 core episodes.

Four signature cells receive repetitions 9-20 under F3:

```text
C1 x NETWORK_PARTITION x RYW
C6 x NETWORK_PARTITION x RYW
C1 x NETWORK_PARTITION x MW
C6 x NETWORK_PARTITION x MW
```

These add 4 x (20 - 8) = 48 histories in 12 episodes. The registered total is
432 histories in 36 fault episodes. The four signature cells therefore have
20 repetitions each; every other cell has eight.

Fault-event intervals are measured with one monotonic clock in the runner
process. The host coordinator's own action timestamps are retained as nested
diagnostics and are not subtracted from runner timestamps. Recovery duration
runs from the recover request through stable topology and final observation of
all initialized histories. Report election attempts, successes, failures, and
timing denominators separately.

Each history places the related client operations around the fault transition:

| Property | Client operations | Checker |
| --- | --- | --- |
| RYW | `W1(x,v1) -> fault transition -> R1(x)` | A read older than the acknowledged W1 is a violation. |
| MR | `W1(x,v1) -> R1(x,v1) -> fault transition -> R2(x)` | A second read older than R1 is a violation. |
| MW | `W1(x) -> fault transition -> W2(x,parent=W1)` | After convergence, W2 visible without W1 is a violation. |
| WFR | `R1(x,vr) -> fault transition -> W2(x,depends_on=vr)` | After convergence, W2 visible without the version returned by R1 is a violation. |

The schedules record unavailable and indeterminate operations as such; they do
not turn them into consistency violations. MR first acknowledges W1 at version
1, then requires R1 to return that write before the fault. This gives the two
subject reads a concrete version that can regress; a missed write or read is a
precondition miss. F3 also records the transition from the former primary to
the majority-side primary. In the four C1/C6 RYW/MW signature cells, start F3
first, launch the four independent W1 operations concurrently, and retain only
operations routed to the former primary during its transient primary interval.
Then wait for the majority-side election and issue R1 or W2 on that side. A C1
`w:1` W1 may be acknowledged and later rolled back; C6 majority W1 may wait or
be unavailable. Record every acknowledged write and retain a later rollback in
the history. The final observer reads the complete logical document directly
from all three members after topology recovery.

The runner groups cases by fault condition and repetition to reduce repeated
fault setup. A grouped episode shares one topology event, so the episode is the
unit for election and recovery timing. Histories within an episode remain
separate keyed workloads and are not treated as independent fault events.
The coordinator persists an active-fault journal before changing topology. If
the host process exits before cleanup, the next `make rq2` invocation must
recover and verify that fault before starting another episode.

Each fault-controller sidecar shares the network namespace of its MongoDB
member. Restarting a member can replace that namespace, so the host coordinator
recreates and health-checks the matching sidecar after every node restart and
before a partition. An episode counts as complete only when the fault action is
verified and recovery converges; a failed action or recovery stops the campaign
and cannot be skipped by `--resume`.

RQ2 is a controlled behavior study, not an estimate of a universal violation
probability. Report the observed count and denominator; for example, report
"no violation was observed in 8 controlled repetitions." Do not report a zero
observed count as a 0% true violation probability. Report p50/p95/p99 operation
latency with the number of operations, and report election/recovery quantiles
with the number of fault episodes behind them.

Pre-registered hypotheses are:

- H2.1: A secondary crash mainly reduces redundancy; operations that remain
  routable should not create new consistency anomalies.
- H2.2: A primary crash mainly appears as election delay and temporary
  unavailability rather than completed but invalid histories.
- H2.3: A primary-isolating partition is the clearest condition for exposing
  differences between weak (`local`/`w:1`) and majority settings, including
  stale state and acknowledged writes later rolled back.
- H2.4: Majority read/write concerns with a causal session should shift
  outcomes from successful but invalid histories toward waiting or temporary
  unavailability.

These are predictions to evaluate, not conclusions. The primary analysis uses
durable client-centric semantics: an acknowledged write later rolled back
after convergence remains part of the recorded history. Discuss MongoDB's
weaker causal-consistency interpretation without durability separately.

Node stop/start and partition operations are orchestrated outside the runner's
container boundary. The runner itself receives no Docker socket. Record fault
start, election, heal/restart, recovery, and cleanup times in the episode and
its histories.

## Provenance and analysis

Before the pilot, freeze configurations, predictions, schedules, schemas, and
this protocol. The campaign manifest records distinct `prediction_commit`,
`protocol_commit`, and `runner_commit`, plus prediction and protocol SHA-256
hashes, actual software versions, and the resolved MongoDB image digest. Setup's
generic source revision is not a substitute for any of those fields. Main
campaigns refuse dirty or missing frozen provenance.

`make analyse` reads only canonical raw histories and campaign manifests. It
recomputes checker outcomes and reports, by property and configuration:

- outcome counts, including all six history outcomes;
- violation rate over `PASS + VIOLATION` only;
- operation success and history completion rates;
- operation latency p50, p95, and p99;
- election and recovery time;
- RC, WC, causal-session contrasts and interactions where the cell data allow.

Contrast formulas are descriptive and exploratory. They do not silently impute
missing cells or claim statistical significance. Analysis and report builds
must work without MongoDB. Generated summaries, plots, macros, and PDFs derive
from raw histories; no observation is copied into LaTeX by hand.

## Reproduction order

```bash
make test
make check-docs
make check-schemas
make setup
make smoke
# freeze protocol and predictions after smoke passes
make pilot
make experiment
# Requires a clean committed protocol and runner with frozen provenance.
make rq2
make rq3
# If interrupted, resume with the same repetitions, seed base, and provenance.
make rq3 RQ3_ARGS=--resume
make rq3-analyse
make analyse
make submission
```

`make test`, documentation/schema checks, analysis, and submission build are
offline. Setup, smoke, pilot, RQ1, RQ2, and RQ3 require Docker Desktop and the
pinned runtime. RQ2 reuses the RQ1 normal baseline and runs no additional
normal histories. RQ3 performs only the three registered mechanism contrasts.
The report describes only the frozen protocol and completed campaigns;
development attempts are not scientific results.

The grouped `make rq2` runner completed on 20 September 2026 with 432 histories
in 36 verified fault episodes. All fault actions were applied and all recovery
checks converged. The host-side coordinator applies node faults and partition
rules through a temporary, narrowly mounted IPC directory; the runner container
receives no Docker socket and runs as a non-root user matching the host-owned
IPC path. See [rq2-results.md](rq2-results.md) for the observed outcomes.

## RQ3 mechanism study: protocol v2

Protocol `rq3-protocol.v2` explains selected RQ1 observations through three
mechanism contrasts. It starts from the six histories in
[`../configs/rq3-anchors.json`](../configs/rq3-anchors.json), then runs five
matched-seed pairs per contrast (30 new histories total). Each arm has its own
namespace, history, and fault episode. These replays explain selected traces;
they are not a third probability-estimation campaign.

The preregistered hypotheses remain outcome-neutral:

| Contrast | Schedule and configurations | Changed setting | Preregistered observation |
| --- | --- | --- | --- |
| M1 | RYW, C5 vs C6 | Causal session off vs on | An isolated stale secondary may return an older version without a causal time bound. The causal arm carries `afterClusterTime` and must not return a causally older successful value. A timeout is no observed response, not proof of an internal server wait. |
| M2 | WFR, C8 vs C5 | Read concern local vs majority | With majority write concern configured and causal sessions off, a local read on the isolated former primary may return its `w:1` setup write; a majority read must not return a value lacking majority acknowledgement. The setup W1 with `w:1` is identical in both arms and is not the subject write. |
| M3 | MW, C3 vs C6 | Write concern `w:1` vs majority | With majority read concern and causal sessions on, an isolated former primary can acknowledge W1 at `w:1` before majority replication. W1 may later be absent after healing. A majority timeout leaves the write effect unresolved until independent post-heal observation. |

### Historical anchors

The six RQ1 histories in `configs/rq3-anchors.json` were selected before the
new replay. Each pair changes only the named configuration setting and records
the relevant route and post-heal state. They are retrospective examples rather
than randomized paired runs: the seeds differ. The M3 histories also follow
different election paths. RQ2 trace candidates were reviewed, but their
operation timing crossed the shared fault boundary differently and did not
provide a better mechanism comparison. The report uses these six histories to
motivate the questions, not as release controls or consistency-rate evidence.

### Fixed topology and normalization

Every contrast uses one named member plan. `P0` is `mongo3`, the initial
primary; `P1` is `mongo2`, the required majority-side primary after a partition
of `P0`; `S` is `mongo1`, the remaining secondary. M1 isolates `S` and routes
its read to `mongo1`. M2 isolates `P0`, reads from `mongo3`, then routes its
dependent write to `mongo2`. M3 routes W1 to `mongo3`, partitions it, then routes
W2 to `mongo2`.

Before each arm, the runner heals and verifies all replica-network partitions,
unfreezes secondary members, waits for all three direct member connections, and
requires three consecutive direct observations of one primary and two
secondaries. It also waits for all three members to report the same last-write
optime for three consecutive polls, both before and after choosing the initial
primary. If `mongo3` is not primary, it freezes other eligible secondaries,
steps down the current primary, waits until direct `hello` observations show
`mongo3` as primary, then unfreezes the held secondaries. It does not use a
fixed sleep as a topology barrier. It then creates version 0 with majority
write concern and waits until all three members independently report only
`init` at version 0. Normalization and the initial-state setup happen before
subject operations and are excluded from their latency. The normalization
step-down command uses a 15-second ineligibility period, which leaves room for
the next arm's bounded election observation when it needs that member to win.

For M2 and M3, `mongo1` is frozen before partitioning `mongo3`. It remains a
voting member but cannot win the election. The runner waits for three
consecutive direct observations of `mongo2` as the majority-side primary and
`mongo1` as its secondary, then unfreezes `mongo1`. MongoDB documents
`replSetFreeze` for controlling election candidacy and `replSetStepDown` for
triggering an election; neither command names a future winner, so the runner
checks the resulting member roles directly. [MongoDB `replSetFreeze`](https://www.mongodb.com/docs/manual/reference/command/replSetFreeze/),
[MongoDB `replSetStepDown`](https://www.mongodb.com/docs/manual/reference/command/replSetStepDown/).

The runner aborts the campaign on a normalization timeout, an unexpected
primary, or an actual command route that differs from the plan. M1 requires
write `mongo3` and read `mongo1`; M2 requires setup W1 and read on `mongo3` plus
dependent write on `mongo2`; M3 requires W1 on `mongo3` and W2 on `mongo2`.
These route checks are experimental controls, not consistency outcomes.

### Pair validity and setup-write instrumentation

Each pair record in `rq3-campaign.v2` includes the desired member and route plan,
per-arm observed roles, isolation target, election winner, actual routes,
initial and semantic pre-state, pair seed, and a strict `control_valid` boolean
with machine-readable failure reasons. Validity requires both histories to
share the preregistered pair seed, schedule, topology plan, and semantic
pre-state; the pair seed is the actual random seed passed to both arms, while
the ordinal identifies each history. Each required direct role and operation
route must match its named member. The two configurations must still differ
only in their registered setting. The analyzer derives validity again from raw
histories instead of trusting the manifest flag. A control-invalid pair is
diagnostic data only: it is not classified as PASS, VIOLATION, UNAVAILABLE, or
INDETERMINATE and does not enter a mechanism outcome denominator.

For M2, setup W1 is sent through the command-monitored direct-member path in
both arms. Its diagnostic records `operation_id: setup_write`, the actual
command's `writeConcern` (including `w: 1`), server address, command events,
and outcome. The arm is invalid if the setup command or its route is missing.
The setup write is a shared stimulus, not the read-concern setting under test.

The report uses only control-valid pairs for mechanism counts and selects the
first preregistered valid pair for its displayed trace; it does not rank pairs
by observed outcomes. It shows planned, valid, and invalid pair counts and keeps
consistency results separate from control failures. A timeout is never evidence
of an internal wait by itself. `afterClusterTime`, command routing, direct role
observations, final document presence, and the returned value remain distinct
observations.

### Preflight, campaign, and acceptance

Unit tests exercise the contrast definitions, anchor hashes, pair identity,
deterministic member plans, normalization failure behavior, resume hashes,
synthetic M1-M3 histories, invalid-pair exclusion, ambiguous write outcomes,
and deterministic analysis output. Before collecting replays,
`make rq3-preflight` runs one topology rehearsal: normalize to `mongo3`,
partition it while `mongo1` is frozen, verify `mongo2` as primary and `mongo1`
as secondary through direct member connections, then heal and normalize again.
The rehearsal is recorded separately and is not an RQ3 history. Every replay
also normalizes and verifies its own planned topology.

Run the v2 workflow from a clean committed checkout:

```bash
make test
make check-docs
make check-schemas
make setup
make rq3-preflight
make rq3
make rq3-analyse
make analyse
make submission
make check-release-ready
```

RQ3 v2 acceptance concerns execution controls, not predicted results:

| Gate | Required |
| --- | ---: |
| Planned and completed histories | 30 / 30 |
| `HARNESS_ERROR` histories | 0 |
| Verified historical anchor histories | 6 / 6 |
| Control-valid pairs for M1, M2, and M3 | 5 / 5 each |
| M1 actual write and read routes | 5 / 5 each |
| M2 actual read and dependent-write routes | 5 / 5 each |
| M3 actual W1 and W2 routes | 5 / 5 each |
| M2 setup W1 command, `w:1`, and `mongo3` route | 10 / 10 arms |
| Topology rehearsal | 1 / 1 |
| Reanalysis of identical raw inputs | byte-identical outputs |
| Report artifacts derived from raw histories | hash-verified |

Consistency observations may take any value. A majority timeout can coexist
with a write that is later observed on every member; the report preserves both
facts. Reanalysis and resume are accepted only when protocol, runner, prediction,
configuration, topology-plan, and preflight hashes match the recorded inputs.
