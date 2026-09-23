# SoC Labs Harness Manager: developer entry points.
#   make venv    create .venv (Python 3.11) and install harness-manager + pyverify (editable)
#   make check   lint + unit + integration (the gate every team runs before handing work back)
#   make web-deps  Playwright for the web UI browser tests (they skip without it)
#   make hil     hardware-in-the-loop read-only tier (board window + lease only)

PY        ?= python3.11
VENV      ?= .venv
PYVERIFY  ?= ../mps3-nanosoc-platform/host/pyverify
BIN        = $(VENV)/bin

.PHONY: venv check lint test hil web-deps clean

venv: $(BIN)/harness-manager

$(BIN)/harness-manager: pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e $(PYVERIFY)
	$(BIN)/pip install -q -e '.[dev]'
	@touch $@

# Web UI browser tests (tests/web): Playwright driving the system Chrome/Chromium.
# Without it those tests skip with the reason.
web-deps: venv
	$(BIN)/pip install -q -e '.[webtest]'

lint: venv
	$(BIN)/ruff check src tests

test: venv
	$(BIN)/pytest -q

check: lint test
	@echo "CHECK PASS"

hil: venv
	HARNESS_MANAGER_HIL=1 $(BIN)/pytest -q -m hil tests/hil

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build src/*.egg-info
