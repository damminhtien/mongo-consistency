PYTHON ?= python3
MC_SMOKE_MOUNT ?= ./results/smoke
.PHONY: check-docs check-schemas check-release-ready submission test setup smoke pilot experiment experiment-fresh rq2 analyse

check-docs:
	$(PYTHON) scripts/check_documentation.py

check-schemas:
	PYTHONPATH=src $(PYTHON) scripts/validate_records.py

check-release-ready:
	$(PYTHON) scripts/check_release_ready.py

submission:
	$(PYTHON) scripts/build_submission.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

setup:
	PYTHONPATH=src $(PYTHON) scripts/setup_experiment.py

smoke: setup
	MC_RESULTS_MOUNT=$(MC_SMOKE_MOUNT) docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign smoke --output-root results/raw --resume

pilot: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign pilot --resume

experiment: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment --resume

experiment-fresh: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment

analyse:
	PYTHONPATH=src $(PYTHON) scripts/analyse_results.py
