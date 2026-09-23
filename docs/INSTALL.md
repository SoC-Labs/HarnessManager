# Installing Harness Manager

The short version is in the [README](../README.md). This page says what the installers
do, how to install from other sources, and how the release is packaged and tested.

## What you need

- Python 3.10 or newer, or [uv](https://docs.astral.sh/uv/). With uv, the installer
  lets uv find a Python 3.10+ or download one.
- git, to clone the repository (it is private: ask SoC Labs for access).
- A network connection to PyPI for the dependencies (FastAPI, uvicorn, websockets,
  cryptography). pyverify comes from `vendor/` in the repository.
- For the app window: pywebview (`--with-app`), or Chrome, Edge, Chromium or Brave.
  Without either, `harness-manager ui` opens a browser tab.

## What the installer does

`scripts/install.sh` (Linux, macOS) and `scripts/install.ps1` (Windows) do the same
steps:

1. Pick the source: this checkout by default, or `--from`.
2. Stop the Harness Manager service if an earlier install is running it, so the
   upgrade does not change files under a running service. A running job (a deploy, an
   SD write) refuses the stop, and then the installer stops too. Wait for the job, then
   run it again.
3. Make the venv, or reuse it when it already holds Python 3.10+.
4. Install the vendored pyverify wheel by its path, then Harness Manager with its
   dependencies (`--find-links vendor`).
5. Write the `harness-manager` command, and check that it runs.

It builds from a temporary copy of the checkout, so your checkout gets no `build/` or
`*.egg-info` directories.

| | Linux and macOS | Windows |
|---|---|---|
| Venv | `~/.local/share/harness-manager/venv` | `%LOCALAPPDATA%\harness-manager\venv` |
| Command | `~/.local/bin/harness-manager` | `%LOCALAPPDATA%\harness-manager\bin\harness-manager.exe` |
| PATH | prints the line to add, if the directory is not on it | adds the directory to your user PATH |
| State (kept by `--uninstall`) | `~/.config/harness-manager` | `%USERPROFILE%\.config\harness-manager` |

Environment variables move these: `HARNESS_MANAGER_HOME` (the venv's parent),
`HARNESS_MANAGER_BIN_DIR` (the command), `HARNESS_MANAGER_STATE_DIR` (the state).

On Linux and macOS the command is a small launcher script. It runs the venv's
`harness-manager`, unless the app's self-update has selected another version in
`<state>/update/app/current.json`; then it runs that one. On Windows the command is a
copy of the venv's `harness-manager.exe`, which holds the venv's absolute path (pipx
does the same).

## Options

| `install.sh` | `install.ps1` | Meaning |
|---|---|---|
| `--from SRC` | `-From SRC` | a checkout, a wheel file, or a git URL (cloned with depth 1) |
| `--ref REF` | `-Ref REF` | the branch or tag to clone, with a git URL |
| `--with-app` | `-WithApp` | the `app` extra: pywebview |
| `--with-serial` | `-WithSerial` | the `serial` extra: pyserial, for the Debug USB serial ports and USB discovery |
| `--python PY` | `-Python PY` | the Python to build the venv with |
| `--no-uv` | `-NoUv` | use venv and pip even when uv is on PATH |
| `--force` | `-Force` | replace a `harness-manager` command the installer did not write |
| `--uninstall` | `-Uninstall` | stop the service, remove the venv and the command |

Examples:

```bash
scripts/install.sh --with-serial                                  # this checkout
scripts/install.sh --from git@github.com:SoC-Labs/HarnessManager.git --ref v0.1.0
scripts/install.sh --from dist/harness_manager-0.1.0-py3-none-any.whl
make install-local INSTALL_ARGS=--with-app                        # the same, from make
```

Extras added once stay installed on later runs; the installer never removes packages.

## Upgrade and uninstall

- **Upgrade:** `git pull`, then run the installer again. It reinstalls Harness Manager
  and the vendored pyverify in the same venv. A pyverify rebuilt from a newer platform
  commit keeps its version number, so the installer always reinstalls it.
- **Uninstall:** `scripts/install.sh --uninstall` (or `-Uninstall`). It stops the
  service, removes the venv and the command, and leaves the state directory: your
  `boards.toml`, SD backups, content store and logs. Delete that yourself if you want it
  gone.

## Without the installer

Any venv works. Install pyverify first, by its path, so the package index never chooses
which pyverify you get:

```bash
python3 -m venv ~/hm && ~/hm/bin/pip install vendor/mps3_pyverify-*.whl
~/hm/bin/pip install --find-links vendor '.[serial]'
```

From a release directory made by `make dist`, which holds both wheels:

```bash
pip install dist/mps3_pyverify-*.whl && pip install --find-links dist harness-manager
```

Do not `pip install harness-manager` or `mps3-pyverify` from PyPI. Neither name is
ours there.

## Packaging (maintainers)

- **Version:** `pyproject.toml` `version`, `harness_manager.__version__` and the newest
  `CHANGELOG.md` heading must agree. `tests/unit/test_l5_release.py` checks it.
- **`make dist`:** builds the sdist, then the wheel from the sdist, into `dist/`; copies
  in the pyverify wheel; writes `dist/SHA256SUMS`.
- **What a user gets:** `tests/integration/test_l5_packaging.py` builds both, installs
  the wheel into a clean venv, and checks the web UI's files (`index.html`, `vendor/`,
  all of them), the MPS3 board-pack entry point, `python -m harness_manager.daemon
  --help`, `harness-manager version` and `pip check`.
- **The installer:** `scripts/smoke_install.sh` (also `make smoke-install`) installs
  into a throwaway HOME, runs the command from PATH, checks the self-update launcher,
  starts `harness-manager ui --demo --no-browser`, fetches the page and its CSP header,
  upgrades in place, and uninstalls. `tests/integration/test_l5_install.py` runs it with
  pip, and with uv when uv is on PATH.

Both integration tests are marked `slow` and `packaging`, take about a minute each, and
skip with the reason when PyPI is unreachable.

## The vendored pyverify

pyverify lives in the platform repo (mps3-nanosoc-platform, `host/pyverify`). Harness
Manager never re-implements the shell protocol, so it needs pyverify at run time.

`vendor/mps3_pyverify-<version>-py3-none-any.whl` is built by:

```bash
make vendor-pyverify                         # ../mps3-nanosoc-platform at HEAD
make vendor-pyverify PLATFORM_REF=<commit>   # a given commit
```

It builds from `git archive <commit> host/pyverify`, so uncommitted edits never reach
the wheel and nothing is written into the platform repo. It pins the wheel's
timestamps to the commit time, so the same commit gives the same sha256. It rewrites
`vendor/README.md` with the commit, the sha256 and the tools used. Re-vendor whenever
Harness Manager starts using a pyverify change, and commit the wheel and the README
together.

`make venv` still installs pyverify editable from `../mps3-nanosoc-platform` when that
checkout is there, so you can change both at once. Without it, or with
`make venv PYVERIFY=`, it installs the vendored wheel.

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

| Job | Runs on | What |
|---|---|---|
| lint | ubuntu-latest | ruff, shellcheck on `scripts/*.sh`, PSScriptAnalyzer on `install.ps1`, including its syntax against Windows PowerShell 5.1 |
| tests | ubuntu-latest, Python 3.10, 3.11, 3.12 | the whole suite: unit, integration, the web UI in the runner's Chrome through Playwright, packaging, and the installer (pip and uv) |
| other-os | windows-latest, macos-latest | `tests/unit`, then the installer end to end (`install.ps1` in Windows PowerShell 5.1; `smoke_install.sh` on macOS) |

The tests job fails if the browser, packaging or installer tests skip, so a missing
Chrome cannot pass silently. pip downloads are cached per job.
