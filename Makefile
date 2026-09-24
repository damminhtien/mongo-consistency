PYTHON ?= python3
MC_SMOKE_MOUNT ?= ./results/smoke
.PHONY: check-docs check-schemas check-release-ready check-generated check-runner-isolation check-submission-artifacts submission test setup smoke pilot experiment experiment-fresh rq2 rq3 rq3-preflight rq3-analyse rq4-analyse mechanism-preflight mechanism-replay analyse-mechanism analyse-partition analyse

check-docs:
	$(PYTHON) scripts/check_documentation.py

check-schemas:
	PYTHONPATH=src $(PYTHON) scripts/validate_records.py

check-release-ready:
	$(PYTHON) scripts/check_release_ready.py

check-generated:
	git diff --exit-code -- .

check-runner-isolation:
	$(PYTHON) scripts/check_runner_isolation.py

check-submission-artifacts:
	PYTHONPATH=src:scripts $(PYTHON) scripts/check_submission_artifacts.py

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

RQ2_ARGS ?=
rq2: setup
	$(PYTHON) scripts/run_rq2_coordinator.py $(RQ2_ARGS)

RQ3_ARGS ?=
MECHANISM_ARGS ?= $(RQ3_ARGS)
rq3: mechanism-replay

mechanism-replay: setup
	docker compose -f compose.yaml run --rm runner scripts/run_rq3_campaign.py $(MECHANISM_ARGS)

rq3-preflight: mechanism-preflight

mechanism-preflight: setup
	docker compose -f compose.yaml run --rm runner scripts/run_rq3_preflight.py

rq3-analyse: analyse-mechanism

analyse-mechanism:
	PYTHONPATH=src $(PYTHON) scripts/analyse_rq3.py

rq4-analyse: analyse-partition

analyse-partition:
	PYTHONPATH=src $(PYTHON) scripts/analyse_rq4.py

analyse:
	@if [ "$(RQ)" = "rq4" ]; then \
		PYTHONPATH=src $(PYTHON) scripts/analyse_rq4.py; \
	else \
		PYTHONPATH=src $(PYTHON) scripts/analyse_results.py; \
	fi
