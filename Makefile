PYTHON ?= python3

.PHONY: check-docs submission test

check-docs:
	$(PYTHON) scripts/check_documentation.py

submission:
	$(PYTHON) scripts/build_submission.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
