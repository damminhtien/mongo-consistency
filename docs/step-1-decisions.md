# Step 1 decisions

These choices are closed unless a reproducibility or safety check shows that a
recorded assumption cannot be implemented.

## Group

- Dam Minh Tien - A0355091E
- Nguyen Minh Duc - Student ID pending
- Vu Nhat Nguyen Thu - Student ID pending

## System and toolchain

- Final database: MongoDB replica set.
- Baseline deployment: three data-bearing members in Docker Compose.
- Target versions: MongoDB 7.0.34, PyMongo 4.18.1, Python 3.14.7,
  Docker Engine 29.8.0, and Docker Compose 5.5.1.
- Setup records actual server and client versions plus the resolved MongoDB
  image digest. The target pins are not a substitute for that runtime evidence.

## Experiment

- Use the C1-C8 matrix of read concern, write concern, and causal session.
- RQ1 studies configuration semantics using property-specific fault schedules.
- RQ2 studies secondary crash, primary crash/election, and primary-isolating
  network partition; it does not repeat normal operation.
- RQ2 uses C1, C3, C4, and C6 for RYW, MR, MW, and WFR. RQ1 normal histories
  for these cells are its descriptive baseline. C5 versus C6 is reserved for
  RQ3.
- RQ2 has eight core repetitions per configuration/fault/property cell. Four
  primary-partition signature cells extend to 20 repetitions. The total is
  432 histories in 36 grouped fault episodes.
- Main RQ1 design: 96 normal controls and 160 adversarial histories, 256 in
  total. The reduced plan keeps three normal and five adversarial repetitions
  in every configuration/property cell so the live campaign remains bounded.
- Pilot design: three adversarial repetitions plus one normal control for each
  configuration/property cell, 128 histories total.
- Run one 32-history machinery smoke before freezing the protocol and predictions.
- Use seed 20260915 plus the campaign ordinal, unique namespaces, and a
  deterministic shuffled plan for RQ1. RQ2 pairs repetition indices across
  fault conditions and gives each history its own namespace and session.

## Protocol

- Use one logical document x with append-only updates and integer application
  versions from one logical writer.
- Record parent_write_id, depends_on_read_id, and depends_on_version.
- Use one explicit PyMongo session per trial, whether causal consistency is ON
  or OFF.
- Set retryReads=false and retryWrites=false.
- Log actual server address, direct role observation, driver-reported role,
  cluster/session timing, concerns, fault state, and operation status.
- Use a five-second operation deadline and a separate 30-second election and
  topology barrier.
- Give RYW, MR, MW, WFR, and normal control distinct schedules.

## Outcomes and analysis

- History outcomes: PASS, VIOLATION, UNAVAILABLE, INDETERMINATE,
  PRECONDITION_MISS, HARNESS_ERROR.
- Only PASS and VIOLATION enter the consistency denominator.
- Treat a write timeout or lost response after command start as INDETERMINATE.
- Report operation success, history completion, latency quantiles, election
  and recovery time, and exploratory factorial contrasts.
- Freeze prediction and protocol commits before the pilot/main results.
- Rebuild summaries and plots offline from raw histories; maintain the report
  prose in the checked-in LaTeX source.

See experimental-protocol.md for the complete experiment definition and
acceptance criteria.
