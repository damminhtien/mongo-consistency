# Quarantined first attempts

This directory preserves 449 initial main-campaign histories from ordinals 697-1280. Each was an adversarial trial whose fault-controller request failed with `Connection refused`; no fault was applied. They are harness failures, not database outcomes, and are excluded from analysis. The canonical analyzer reads histories under `results/raw/` only.

The Compose fix in commit `7494c84` makes each worker wait for all three fault controllers to become healthy. All 449 cases were rerun with the same ordinals and deterministic seeds. The corrected histories are in `results/raw/experiment/`; the 449 first attempts remain here for audit and are not part of any metric. `archive-manifest.json` records each quarantined file's hash, trial metadata, and byte-equal worker staging copy.
