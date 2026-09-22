PYTHON ?= python3
SOURCE_DIR ?= source
MC_SMOKE_MOUNT ?= ./results/smoke

.PHONY: setup smoke pilot experiment experiment-fresh rq2 rq3-preflight rq3

setup:
	cd $(SOURCE_DIR) && PYTHONPATH=src $(PYTHON) scripts/setup_experiment.py

smoke: setup
	cd $(SOURCE_DIR) && MC_RESULTS_MOUNT=$(MC_SMOKE_MOUNT) docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign smoke --output-root results/raw --resume

pilot: setup
	cd $(SOURCE_DIR) && docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign pilot --resume

experiment: setup
	cd $(SOURCE_DIR) && docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment --resume

experiment-fresh: setup
	cd $(SOURCE_DIR) && docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment

RQ2_ARGS ?=
rq2: setup
	cd $(SOURCE_DIR) && $(PYTHON) scripts/run_rq2_coordinator.py $(RQ2_ARGS)

rq3-preflight: setup
	cd $(SOURCE_DIR) && docker compose -f compose.yaml run --rm runner scripts/run_rq3_preflight.py

RQ3_ARGS ?=
rq3: setup
	cd $(SOURCE_DIR) && docker compose -f compose.yaml run --rm runner scripts/run_rq3_campaign.py $(RQ3_ARGS)
