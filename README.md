# MongoDB Client-Centric Consistency Experiments

Repository for the DSA5208 group project on client-centric consistency in a MongoDB replica set.

The repository currently contains the project brief, design notes, and execution checklist. No cluster run or experiment result has been recorded yet.

## Files

- [Assignment brief](docs/assignment.md): required work and submission material.
- [Project plan](docs/project-plan.md): system choice, research questions, variables, and limits.
- [Experimental protocol](docs/experimental-protocol.md): history format, checkers, fault cases, and metrics.
- [Report plan](docs/report-plan.md): report sections, figures, tables, and reproduction material.
- [Execution checklist](TODO.md): tasks in the order they should be done.

## Source

The documents were prepared from [DSA5208 Scalable Distributed GRP Project](https://docs.google.com/document/d/1X4Lq5Za8d1jb-YOE-K-uBAOfwCax-soaJb-1WFeHJ80/edit), read on 15 September 2026. The Google Doc contains both the course brief and the agreed project plan. Use that document if a requirement here is unclear.

## Intended commands

```bash
make setup
make experiment
make analyse
```

These commands are part of the target interface. The implementation has not been added yet.

## Documentation checks

Run `make check-docs` after changing Markdown, text, LaTeX, or PDF files. The checker scans the repository recursively, skips local agent metadata and tool caches, and checks prose, links, repeated structure, and unsupported claims. For PDFs it also runs `pdfinfo` and `pdftotext -layout`.

Run `make test` to run the checker tests without requiring a MongoDB cluster.
