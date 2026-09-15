PYTHON ?= python3

.PHONY: check-docs test

check-docs:
	$(PYTHON) scripts/check_documentation.py

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
