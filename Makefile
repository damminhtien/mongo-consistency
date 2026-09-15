PYTHON ?= python3

.PHONY: check-docs submission test setup pilot experiment

check-docs:
	$(PYTHON) scripts/check_documentation.py

submission:
	$(PYTHON) scripts/build_submission.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

setup:
	PYTHONPATH=src $(PYTHON) scripts/setup_experiment.py

pilot:
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign pilot

experiment:
	docker compose -f compose.yaml run --rm runner scripts/run_campaign.py --campaign experiment
