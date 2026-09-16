PYTHON ?= python3

.PHONY: check-docs check-schemas submission test setup pilot experiment experiment-fresh analyse

check-docs:
	$(PYTHON) scripts/check_documentation.py

check-schemas:
	PYTHONPATH=src $(PYTHON) scripts/validate_records.py

submission:
	$(PYTHON) scripts/build_submission.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

setup:
	PYTHONPATH=src $(PYTHON) scripts/setup_experiment.py

pilot: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign pilot --resume

experiment: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment --resume

experiment-fresh: setup
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment

analyse:
	PYTHONPATH=src $(PYTHON) scripts/analyse_results.py
