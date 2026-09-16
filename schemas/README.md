# Record schemas

The JSON files in this directory describe the records exchanged by the runner
and the offline analysis. The files are versioned with the raw-history format;
they are part of the submission source and are not generated from a run.

The executable check is:

```bash
make check-schemas
```

The checker validates raw histories, campaign manifests, setup metadata, and
derived summaries when those files exist. An empty `results/raw/` directory is
valid and produces a `NO_DATA` analysis state.
