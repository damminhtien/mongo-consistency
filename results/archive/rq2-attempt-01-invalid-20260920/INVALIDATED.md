# Invalid RQ2 attempt 01

This attempt wrote the planned 432 history files, but it is not a valid completed campaign. The `COMPLETE` status in its manifest was emitted by the pre-fix runner and is incorrect.

In F3/r02, the target MongoDB node had been restarted while its fault-controller sidecar remained attached to the prior network namespace. Partition application failed with a connection reset. Later episodes did not apply their registered faults, but the runner continued and recorded them. Do not include any history from this attempt in RQ2 analysis.

The controller lifecycle and fail-closed completion checks were corrected in commit `3c35e94`. The canonical rerun belongs under `results/raw/rq2/`.
