# MongoDB Client-Centric Consistency Experiments

This repository contains the planning and reproducibility material for the DSA5208 group project on client-centric consistency in a MongoDB replica set.

Current status: planning only. No experiment result or universal consistency guarantee is claimed by these documents.

## Documents

- [Assignment brief](docs/assignment.md) — normalized requirements, deliverables, and deadline.
- [Project plan](docs/project-plan.md) — technology decision, thesis, research questions, variables, metrics, and scope.
- [Experimental protocol](docs/experimental-protocol.md) — history schema, executable predicates, fault schedules, and outcome rules.
- [Report and reproducibility plan](docs/report-plan.md) — report structure, figures, tables, evidence standards, and expected artifacts.

## Source and provenance

These documents were normalized from the Google Drive document [DSA5208 Scalable Distributed GRP Project](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), read on 2026-09-15. The source document contains both the assignment brief and the agreed working plan.

The source remains authoritative for the course requirements. This repository is the working record for implementation decisions and experimental evidence; it must not turn planned predictions into post-hoc claims.

## Planned workflow

1. `make setup` — start and initialize the three-node replica set.
2. `make experiment` — run selected adversarial histories and record raw traces.
3. `make analyse` — run independent checkers and generate summaries/figures.

The commands and implementation do not exist yet. They are the first delivery target described by the plan.
