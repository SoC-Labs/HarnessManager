# SoC Labs Harness Manager: developer entry points.
#   make venv    create .venv (Python 3.11): harness-manager editable, pyverify from the
#                VENDORED WHEEL (what the app ships with). A development pyverify only when
#                asked: make venv DEV_PYVERIFY=1 (../mps3-nanosoc-platform/host/pyverify) or
#                make venv PYVERIFY=<dir>; then `make release` refuses unless ALLOW_DEV_PYVERIFY=1.
#                Every target that needs the venv runs tools/venv_guard.py first: it refuses a
#                VENV outside this checkout, a harness_manager that does not load from this
#                checkout's src (or two editable links), and (release) a non-vendored pyverify.
#   make check   lint + tokens-check + unit + integration (the gate every team runs before
#                handing work back)
#   make tokens  regenerate tokens.css, design/generated/* from design/tokens.json
#   make tokens-check  fail if they are stale, or app.css has colours of its own (in check)
#   make web-deps  Playwright for the web UI browser tests (they skip without it)
#   make hil     hardware-in-the-loop read-only tier (board window + lease only)
#   make hil-auto HIL_ARGS='--plan linux-netboot --board 192.168.10.101 --evidence DIR'
#                the HIL runbooks unattended (docs/HIL_AUTO.md; david runs it, with the lease)
#
# Release (lane L5):
#   make dist            sdist + wheel in dist/, with the vendored pyverify wheel, constraints.txt
#                        and SHA256SUMS
#   make install-local   install this checkout for your user (scripts/install.sh; INSTALL_ARGS=...)
#   make smoke-install   install, run and uninstall in a throwaway HOME (scripts/smoke_install.sh)
#   make wheelhouse      every wheel for an offline install, in dist/wheelhouse (WHEELHOUSE_ARGS=...)
#   make lock            re-pin constraints.txt (needs uv; LOCK_ARGS=--upgrade moves every pin)
#   make vendor-pyverify rebuild vendor/mps3_pyverify-*.whl from $(PLATFORM) at PLATFORM_REF
#
# Signed releases (lane OTA-R; docs/RELEASING.md, keys: docs/KEYS.md). Each is a DRY RUN into
# dist/release/ (written, signed, verified; the gh steps only written) unless PUBLISH=1.
# The key source: HM_RELEASE_SECRET_KEY + HM_RELEASE_PUBLIC_KEY (minisign CLI), or RELEASE_ARGS.
#   make release [VERSION=] [CHANNEL=beta] [MIRROR=DIR] [PUBLISH=1]     the app (wheel, lock, dep)
#   make release-harness BUNDLE=DIR VERSION=1.2.0 [CHANNEL=beta]       a harness bundle (H13)
#   make release-promote VERSION=0.2.0 [CATALOG=hm-app]                beta -> stable, re-signed

PY        ?= python3.11
VENV      ?= .venv
PLATFORM  ?= ../mps3-nanosoc-platform
PLATFORM_REF ?= HEAD
# pyverify comes from the VENDORED WHEEL by default, because that is what the app ships with.
# The sibling platform checkout is a dev tree on whatever branch other work left it on; linking
# it silently broke the app twice (Oct 2026). Editable from it only on request:
#   make venv DEV_PYVERIFY=1                     $(PLATFORM)/host/pyverify
#   make venv PYVERIFY=../x/host/pyverify        any other directory
# Going back: `make clean venv`, or `pip install --force-reinstall vendor/mps3_pyverify-*.whl`.
DEV_PYVERIFY ?=
PYVERIFY  ?= $(if $(DEV_PYVERIFY),$(PLATFORM)/host/pyverify,)
ALLOW_DEV_PYVERIFY ?=
GUARD      = $(PY) tools/venv_guard.py
PYVERIFY_WHEEL = $(firstword $(wildcard vendor/mps3_pyverify-*.whl))
INSTALL_ARGS ?=
WHEELHOUSE_ARGS ?=
LOCK_ARGS ?=
RELEASE_ARGS ?=
CHANNEL   ?= beta
CATALOG   ?= hm-app
BIN        = $(VENV)/bin
RELEASE    = $(BIN)/python -m tools.release
RELEASE_COMMON = $(if $(MIRROR),--mirror $(MIRROR)) $(if $(PUBLISH),--publish) $(RELEASE_ARGS)

.PHONY: venv check lint test hil hil-auto web-deps clean dist install-local smoke-install vendor-pyverify \
	wheelhouse lock release-guard release release-harness release-promote tokens tokens-check

# The guard runs on every `make venv` and everything that depends on it (check, release, ...),
# not only when the venv is rebuilt: a venv another checkout re-pointed must not pass quietly.
venv: $(BIN)/harness-manager
	@$(GUARD) harness --venv $(VENV) --checkout $(CURDIR)

# __init__.py holds the version (pyproject reads it): a bump refreshes the editable metadata.
# This rule runs `pip install -e .`, so the venv must belong to this checkout.
$(BIN)/harness-manager: pyproject.toml src/harness_manager/__init__.py
	@$(GUARD) path --venv $(VENV) --checkout $(CURDIR)
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(if $(PYVERIFY),$(BIN)/pip install -q -e $(PYVERIFY),$(BIN)/pip install -q $(PYVERIFY_WHEEL))
	$(BIN)/pip install -q --find-links vendor -e '.[dev]'
	@touch $@

# Web UI browser tests (tests/web): Playwright driving the system Chrome/Chromium.
# Without it those tests skip with the reason.
web-deps: venv
	@$(GUARD) path --venv $(VENV) --checkout $(CURDIR)
	$(BIN)/pip install -q --find-links vendor -e '.[webtest]'

lint: venv
	$(BIN)/ruff check src tests tools/release tools/gen_tokens.py tools/gen_panel_codes.py tools/hil
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/*.sh; \
	else echo "lint: shellcheck not found, scripts/*.sh not checked"; fi

test: venv
	$(BIN)/pytest -q

# Design tokens (decision P4): design/tokens.json is the one source; tools/gen_tokens.py
# (stdlib only) writes the web UI's tokens.css and design/generated/{palette.json,clcd_palette.h,
# clcd_glyphs.h} (the panel's extension glyphs 0x80-0x86, from its CLCD_GLYPHS table).
tokens: venv
	$(BIN)/python tools/gen_tokens.py

tokens-check: venv
	$(BIN)/python tools/gen_tokens.py --check

check: lint tokens-check test
	@echo "CHECK PASS"

hil: venv
	HARNESS_MANAGER_HIL=1 $(BIN)/pytest -q -m hil tests/hil

# The runbooks as an unattended runner (lane HIL-AUTO). Nothing here takes a lease.
HIL_ARGS ?=
hil-auto: venv
	$(BIN)/python -m tools.hil run $(HIL_ARGS)

# The sdist, then the wheel built from it (python -m build), plus the pyverify wheel the
# install needs, so dist/ alone installs: pip install dist/mps3_pyverify-*.whl, then
# pip install dist/harness_manager-*.whl (by file name, never by name from PyPI).
dist: venv
	rm -rf dist
	$(BIN)/python -m build --outdir dist .
	cp $(PYVERIFY_WHEEL) constraints.txt dist/
	cd dist && $(CURDIR)/$(BIN)/python -c "import hashlib, pathlib; \
	print(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' \
	for p in sorted(pathlib.Path('.').iterdir()) if p.name != 'SHA256SUMS'), end='')" > SHA256SUMS
	@cat dist/SHA256SUMS

install-local:
	scripts/install.sh --from . $(INSTALL_ARGS)

smoke-install:
	scripts/smoke_install.sh $(INSTALL_ARGS)

# Build it with the Python version of the machine you will install on (--python PY).
wheelhouse:
	scripts/make_wheelhouse.sh $(WHEELHOUSE_ARGS) dist/wheelhouse

lock:
	scripts/lock_deps.sh $(LOCK_ARGS)

vendor-pyverify:
	PYTHON=$(if $(wildcard $(BIN)/python),$(BIN)/python,python3) scripts/vendor_pyverify.sh $(PLATFORM) $(PLATFORM_REF)

# The release is tested and built against the pyverify it ships: refuse a development one.
release-guard: venv
	@$(GUARD) pyverify --venv $(VENV) --wheel $(PYVERIFY_WHEEL) $(if $(ALLOW_DEV_PYVERIFY),--allow-dev)

release: release-guard
	$(RELEASE) app $(if $(VERSION),--version $(VERSION)) --channel $(CHANNEL) $(RELEASE_COMMON)

release-harness: venv
	$(RELEASE) harness --bundle $(BUNDLE) --version $(VERSION) --channel $(CHANNEL) \
		$(RELEASE_COMMON)

release-promote: venv
	$(RELEASE) promote --catalog $(CATALOG) --version $(VERSION) $(RELEASE_COMMON)

# --- RELEASE-PIPE --- a harness release from the PLATFORM's artifacts (docs/RELEASING.md,
# "Publishing a harness release"): assembled, signed and verified into OUT (default
# dist/release). It never publishes: scripts/publish_harness_release.sh does (dry run first).
#   make harness-release VERSION=2.0.0 FROM=<mint prod dir> KEY=<release.key> | TEST_KEY=1
#        [BIT=<.bit> STAGE0_BAKE=<bake.json>] [SD_TEMPLATES=<dir> | SD=<tree>] [OVERLAYS=<dir>]
#        [INCLUDE_AAA=1] [KIT=<zip>] [REPO=OWNER/REPO] [OUT=<dir>] [CHANNEL=beta]
#        [BOARD_REVS=HBI0309B,HBI0309C]  (FIX-PACK-9: the default; the SD serves both revisions)
.PHONY: harness-release
harness-release: venv
	$(RELEASE) harness-release --version $(VERSION) --from $(FROM) --channel $(CHANNEL) \
		$(if $(SD),--sd $(SD),--sd-templates $(or $(SD_TEMPLATES),$(PLATFORM)/fpga/mps3_sd/templates)) \
		$(if $(BIT),--bit $(BIT)) $(if $(STAGE0_BAKE),--stage0-bake $(STAGE0_BAKE)) \
		$(if $(OVERLAYS),--overlays $(OVERLAYS)) $(if $(INCLUDE_AAA),--include-aaa) \
		$(if $(KIT),--kit $(KIT)) $(if $(KEY),--key $(KEY)) $(if $(TEST_KEY),--test-key) \
		$(if $(REPO),--repo $(REPO)) $(if $(OUT),--out $(OUT)) \
		$(if $(MIRROR),--mirror $(MIRROR)) $(if $(BOARD_REVS),--board-rev $(BOARD_REVS)) \
		$(RELEASE_ARGS)

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build dist src/*.egg-info
