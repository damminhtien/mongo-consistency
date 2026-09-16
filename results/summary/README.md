# Derived summaries

`make analyse` generates tables and metrics from `results/raw/` without a live
MongoDB connection. It records the checker version, input history hashes,
setting, fault condition, repetition count, denominator, operation success,
history completion, latency, election, and recovery metrics.

An empty raw-history directory produces an explicit `NO_DATA` summary with zero
outcome counts and null rate and timing fields.
