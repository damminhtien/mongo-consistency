# Experiment configurations

Store versioned settings, workload parameters, repetition counts, seeds, predictions, and fault schedules here. A configuration used by a trial must be copied or referenced by its trial manifest.

The committed files are `configurations.json`, `predictions.json`, `schedules.json`, and `campaign.json`. They use JSON so the offline tools can load them with the Python standard library. The prediction manifest is frozen before result files are generated.
