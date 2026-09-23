# SoC Labs Harness Manager: developer entry points.
#   make venv    create .venv (Python 3.11) and install harness-manager + pyverify (editable)
#   make check   lint + unit + integration (the gate every team runs before handing work back)
#   make web-deps  Playwright for the web UI browser tests (they skip without it)
#   make hil     hardware-in-the-loop read-only tier (board window + lease only)
#
# Release (lane L5):
#   make dist            sdist + wheel in dist/, with the vendored pyverify wheel and SHA256SUMS
#   make install-local   install this checkout for your user (scripts/install.sh; INSTALL_ARGS=...)
#   make smoke-install   install, run and uninstall in a throwaway HOME (scripts/smoke_install.sh)
#   make vendor-pyverify rebuild vendor/mps3_pyverify-*.whl from $(PLATFORM) at PLATFORM_REF

PY        ?= python3.11
VENV      ?= .venv
PLATFORM  ?= ../mps3-nanosoc-platform
PLATFORM_REF ?= HEAD
# pyverify comes from the platform checkout next to this one, editable, when it is there
# (you co-develop both). Without it, from the vendored wheel. `make venv PYVERIFY=` forces
# the wheel.
PYVERIFY  ?= $(if $(wildcard $(PLATFORM)/host/pyverify/pyproject.toml),$(PLATFORM)/host/pyverify,)
PYVERIFY_WHEEL = $(firstword $(wildcard vendor/mps3_pyverify-*.whl))
INSTALL_ARGS ?=
BIN        = $(VENV)/bin

.PHONY: venv check lint test hil web-deps clean dist install-local smoke-install vendor-pyverify

venv: $(BIN)/harness-manager

$(BIN)/harness-manager: pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(if $(PYVERIFY),$(BIN)/pip install -q -e $(PYVERIFY),$(BIN)/pip install -q $(PYVERIFY_WHEEL))
	$(BIN)/pip install -q --find-links vendor -e '.[dev]'
	@touch $@

# Web UI browser tests (tests/web): Playwright driving the system Chrome/Chromium.
# Without it those tests skip with the reason.
web-deps: venv
	$(BIN)/pip install -q --find-links vendor -e '.[webtest]'

lint: venv
	$(BIN)/ruff check src tests
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/*.sh; \
	else echo "lint: shellcheck not found, scripts/*.sh not checked"; fi

test: venv
	$(BIN)/pytest -q

check: lint test
	@echo "CHECK PASS"

hil: venv
	HARNESS_MANAGER_HIL=1 $(BIN)/pytest -q -m hil tests/hil

# The sdist, then the wheel built from it (python -m build), plus the pyverify wheel the
# install needs: `pip install --find-links dist harness-manager` works from dist/ alone.
dist: venv
	rm -rf dist
	$(BIN)/python -m build --outdir dist .
	cp $(PYVERIFY_WHEEL) dist/
	cd dist && $(CURDIR)/$(BIN)/python -c "import hashlib, pathlib; \
	print(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' \
	for p in sorted(pathlib.Path('.').iterdir()) if p.name != 'SHA256SUMS'), end='')" > SHA256SUMS
	@cat dist/SHA256SUMS

install-local:
	scripts/install.sh --from . $(INSTALL_ARGS)

smoke-install:
	scripts/smoke_install.sh $(INSTALL_ARGS)

vendor-pyverify:
	PYTHON=$(if $(wildcard $(BIN)/python),$(BIN)/python,python3) scripts/vendor_pyverify.sh $(PLATFORM) $(PLATFORM_REF)

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build dist src/*.egg-info
