# Generated figures

`make analyse` generates the plots from canonical raw histories and copies the
same files to `submission/figures/`. Each data figure is tied to the setting,
fault condition, property, and latency statistic represented in the summary.

When `results/raw/` is empty, the generated files contain `NO_DATA` and must not
be read as measurements.
