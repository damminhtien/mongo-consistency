# Raw histories

Store one structured history and its trial manifest for each run. Keep successful, violating, and unavailable operations in the raw record. Do not replace a failed trial with a later retry.

The committed `pilot/` set contains 192 histories: 160 adversarial cases and 32 normal controls, using the source revision and runtime recorded in `results/setup/toolchain.json`. Main campaign histories use the same layout under `experiment/`.
