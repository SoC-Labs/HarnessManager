# SoC Labs Harness Manager: developer entry points.
#   make venv    create .venv (Python 3.11) and install socharness + pyverify (editable)
#   make check   lint + unit + integration (the gate every team runs before handing work back)
#   make hil     hardware-in-the-loop read-only tier (board window + lease only)

PY        ?= python3.11
VENV      ?= .venv
PYVERIFY  ?= ../mps3-nanosoc-platform/host/pyverify
BIN        = $(VENV)/bin

.PHONY: venv check lint test hil gui-deps gui-syslibs clean

venv: $(BIN)/socharness

$(BIN)/socharness: pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e $(PYVERIFY)
	$(BIN)/pip install -q -e '.[dev]'
	@touch $@

gui-deps: venv
	$(BIN)/pip install -q -e '.[guitest]'

# Qt >= 6.5 needs libxcb-cursor.so.0 for X11. RHEL 8 hosts often lack it. This
# fetches the distro package WITHOUT root and unpacks the library into the
# venv; socharness-gui preloads it from there automatically.
SYSLIBS = $(VENV)/lib/socharness-syslibs
gui-syslibs: venv
	@if /sbin/ldconfig -p 2>/dev/null | grep -q libxcb-cursor.so.0; then \
	  echo "libxcb-cursor already provided by the system"; \
	else \
	  tmp=$$(mktemp -d) && cd $$tmp && \
	  dnf download -q --setopt=cachedir=$$tmp/cache --destdir=$$tmp xcb-util-cursor && \
	  rpm2cpio xcb-util-cursor-*.rpm | cpio -idm --quiet && \
	  mkdir -p $(CURDIR)/$(SYSLIBS) && cp -a usr/lib64/libxcb-cursor.so.0* $(CURDIR)/$(SYSLIBS)/ && \
	  rm -rf $$tmp && echo "installed libxcb-cursor into $(SYSLIBS)"; \
	fi

lint: venv
	$(BIN)/ruff check src tests

test: venv
	$(BIN)/pytest -q

check: lint test
	@echo "CHECK PASS"

hil: venv
	SOCHARNESS_HIL=1 $(BIN)/pytest -q -m hil tests/hil

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build src/*.egg-info
