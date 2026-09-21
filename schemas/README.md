# Record schemas

The JSON files in this directory define trial histories, nested operation and
fault records, checker outcomes, campaign manifests, and analysis summaries.
RQ3 has a checked historical-anchor selection schema plus protocol-v2 campaign,
topology-rehearsal, analysis, and trace-selection schemas. These files are
versioned with their record formats and are submission source, not generated
from a run.

The executable check is:

```bash
make check-schemas
```

The checker validates each schema against Draft 2020-12, resolves local schema
references, then validates raw histories, campaign manifests, the six selected
RQ1 anchor records, RQ3 topology-rehearsal records, derived checker outcomes,
and summaries when present. It also checks history hashes, RQ3 pair-control
plan invariants, and analysis and selection artifacts when present. An empty
`results/raw/` directory is valid and produces a `NO_DATA` analysis state.
