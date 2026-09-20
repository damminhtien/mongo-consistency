# Record schemas

The JSON files in this directory define trial histories, nested operation and
fault records, checker outcomes, campaign-run manifests, and analysis summaries.
They are versioned with the raw-history format and are submission source, not
generated from a run.

The executable check is:

```bash
make check-schemas
```

The checker validates each schema against Draft 2020-12, resolves local schema
references, then validates raw histories, campaign manifests, derived checker
outcomes, and summaries when present. It also checks history hashes and
campaign-plan invariants. An empty `results/raw/` directory is valid and
produces a `NO_DATA` analysis state.
