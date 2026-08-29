.PHONY: setup generate feed diagnose run report test clean

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip

setup:
	python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	@echo ""
	@echo "Setup done. Now: cp .env.example .env  and fill in your keys."

generate:
	$(PY) -m dunning.generate

feed:
	$(PY) -m dunning.feed

diagnose:
	$(PY) -m dunning.diagnose

run:
	$(PY) -m dunning.run_batch

report:
	@open reports/latest.html 2>/dev/null || echo "No report yet - run 'make run' first."

test:
	$(PY) -m pytest -q

clean:
	rm -rf $(VENV) __pycache__ dunning/__pycache__ data/*.json data/*.jsonl reports/*.html logs
