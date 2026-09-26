# Installing Harness Manager

The short version is in the [README](../README.md). This page says what the installers
do, how to install from other sources, and how the release is packaged and tested.

## What you need

- Python 3.10 or newer, or [uv](https://docs.astral.sh/uv/). The installer takes the
  newest Python 3.10+ it finds (`--python` names one). With uv and no such Python, uv
  downloads one, with no root.
- git, to clone the repository (it is private: ask SoC Labs for access).
- A network connection to PyPI for the dependencies (FastAPI, uvicorn, websockets,
  cryptography), directly or through a proxy. pyverify comes from `vendor/`. With no
  network, see [No network](#no-network-a-wheelhouse).
- For the app window: Chrome, Chromium, Edge or Brave. Without one, `harness-manager app`
  opens a browser tab. No system library is needed: every dependency is a wheel.

### Linux prerequisites, by distribution

The CI install matrix (`.github/workflows/install-matrix.yml`) installs exactly these on
each distribution's own container image, then runs the installer, the demo UI and an
upgrade and uninstall. Each needs only these packages and nothing else.

| Distribution | Its Python | Install first |
|---|---|---|
| Rocky, RHEL, Alma 8 | `python3` is 3.6: too old | `sudo dnf install -y python3 python3.12 expat git tar` (the installer picks python3.12; `python3` is only there to prove it). `expat` updates the library: an un-updated RHEL 8 has one older than python3.12 needs, and its venv then has no pip |
| Rocky, RHEL, Alma 9 | `python3` is 3.9: too old | `sudo dnf install -y python3.12 git-core tar` |
| Rocky 9 with no Python 3.10+ and no root | 3.9 | `sudo dnf install -y git-core tar` once, then uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Ubuntu 22.04 | 3.10 | `sudo apt-get update && sudo apt-get install -y --no-install-recommends python3 python3-venv git curl ca-certificates` |
| Ubuntu 24.04 | 3.12 | the same as Ubuntu 22.04 |
| Debian 12 | 3.11 | the same as Ubuntu 22.04 |
| Fedora (current) | 3.13 or newer | `sudo dnf install -y python3 git-core tar` |

On Debian and Ubuntu, `python3-venv` is the one people miss; on RHEL 8 it is an
out-of-date `expat`. Without them the venv has no pip. The installer then shows the real
cause (venv hides it) and names the package for your distribution. Older releases (Ubuntu 20.04, Debian 11) have no Python
3.10 package: use uv. On a system whose `python3` is too old, the installer lists what it
found and prints the line for your distribution.

### From clone to the app window

About a minute on a fresh machine with the prerequisites, most of it the download:

```bash
git clone git@github.com:SoC-Labs/HarnessManager.git   # 1. clone (private: SSH key)
HarnessManager/scripts/install.sh                       # 2. install (~20 s; ~5 s with uv)
harness-manager app --demo                              # 3. the app, with demo boards
```

If step 3 says `command not found`, your `~/.local/bin` is not on PATH yet: the installer
printed the line to add, and the full path to run until then.

## What the installer does

`scripts/install.sh` (Linux, macOS) and `scripts/install.ps1` (Windows) do the same
steps:

1. Check it can write where it installs, and take a lock, so two installs at once
   cannot interleave (the second one says which process holds it and exits).
2. Pick the source: this checkout by default, or `--from`.
3. Stop the Harness Manager service if an earlier install is running it, so the
   upgrade does not change files under a running service. A running job (a deploy, an
   SD write) refuses the stop, and then the installer stops too. Wait for the job, then
   run it again.
4. Make the venv, or reuse it when it already holds Python 3.10+.
5. Install the vendored pyverify wheel by its path, then Harness Manager with its
   dependencies (`--find-links vendor`), at the versions pinned in `constraints.txt`.
6. Write the `harness-manager` command, and check that it runs.
7. On Linux, add Harness Manager to the desktop's application menu.
8. Remember the extras and the menu choice in `install.conf`, so a re-run keeps them.
9. Put uv in the venv, because the app's self-update builds each new version with it.
   Without network (a wheelhouse with no uv wheel) it says so and carries on.
10. Record the install in `install.json`, and register the venv in the self-update
    pointer (see [Self-update and the install root](#self-update-and-the-install-root)).

It builds from a temporary copy of the checkout, so your checkout gets no `build/` or
`*.egg-info` directories.

| | Linux and macOS | Windows |
|---|---|---|
| Venv | `~/.local/share/harness-manager/venv` | `%LOCALAPPDATA%\harness-manager\venv` |
| Command | `~/.local/bin/harness-manager` | `%LOCALAPPDATA%\harness-manager\bin\harness-manager.exe` |
| PATH | prints the line to add, for your shell, if the directory is not on it | adds the directory to your user PATH |
| Menu entry (Linux) | `~/.local/share/applications/harness-manager.desktop`, icon in `~/.local/share/icons/hicolor/scalable/apps/` | none yet |
| Choices a re-run keeps | `~/.local/share/harness-manager/install.conf` | the extras, in `install.json` |
| Install record, self-update pointer | `~/.local/share/harness-manager/install.json`, `current.json` | `%LOCALAPPDATA%\harness-manager\install.json`, `current.json` |
| Self-updated versions (removed by `--uninstall`) | `~/.local/share/harness-manager/versions/` | `%LOCALAPPDATA%\harness-manager\versions\` |
| State (kept by `--uninstall`) | `~/.config/harness-manager` | `%USERPROFILE%\.config\harness-manager` |

Environment variables move these: `HARNESS_MANAGER_HOME` (the venv's parent),
`HARNESS_MANAGER_BIN_DIR` (the command), `HARNESS_MANAGER_STATE_DIR` (the state).

The command is the venv's `harness-manager-launch`: on Linux and macOS through a small
shell script, on Windows as a copy of `harness-manager-launch.exe`, which holds the venv's
absolute path (pipx does the same). It runs the version the app's self-update selected,
else the installed one. [Self-update and the install root](#self-update-and-the-install-root)
says how.

## Options

| `install.sh` | `install.ps1` | Meaning |
|---|---|---|
| `--from SRC` | `-From SRC` | a checkout, a wheel file, or a git URL (cloned with depth 1) |
| `--ref REF` | `-Ref REF` | the branch or tag to clone, with a git URL |
| `--with-app` | `-WithApp` | the `app` extra: pywebview, a native window on Windows and macOS. On Linux it also needs GTK or Qt bindings, which a venv lacks, so Linux uses the Chrome/Chromium app window |
| `--with-serial` | `-WithSerial` | the `serial` extra: pyserial, for the Debug USB serial ports and USB discovery |
| `--python PY` | `-Python PY` | the Python to build the venv with |
| `--no-uv` | `-NoUv` | use venv and pip even when uv is on PATH |
| `--offline DIR` | | install only from a wheelhouse DIR; never contact the package index |
| `--latest` | | the newest dependency versions instead of the pins (rebuilds the venv) |
| `--no-desktop` / `--desktop` | | skip the Linux menu entry (remembered), or bring it back |
| `--force` | `-Force` | replace a `harness-manager` command the installer did not write |
| `--uninstall` | `-Uninstall` | stop the service, remove the venv, the self-updated versions, the command and the menu entry |

Examples:

```bash
scripts/install.sh --with-serial                                  # this checkout
scripts/install.sh --from git@github.com:SoC-Labs/HarnessManager.git --ref v0.1.0
scripts/install.sh --from dist/harness_manager-0.1.0-py3-none-any.whl
make install-local INSTALL_ARGS=--with-app                        # the same, from make
```

Extras added once stay installed on later runs, even if the venv is rebuilt: the
installer remembers them. It never removes packages.

## The desktop menu (Linux)

On Linux the installer adds **Harness Manager** to your desktop's application menu:
`~/.local/share/applications/harness-manager.desktop` and its icon. It runs
`harness-manager app`; its second action, **Open with Demo Boards**, runs
`harness-manager app --demo`. Its window class matches the app window's, so the running
app groups under the menu icon in the dock or taskbar.

- **A server with no display** (you install over ssh): the entry is written anyway. It
  is a small file that nothing reads until a desktop session starts, and a remote
  desktop (ThinLinc, VNC, X2Go) on the same machine shows it then.
- **ThinLinc and D-Bus:** the app window from the menu starts without the desktop's
  D-Bus session bus, as it does from a terminal, so it is not blank on ThinLinc.
  `HARNESS_MANAGER_APP_KEEP_DBUS=1` in your session keeps the bus.
- **No Chrome or Chromium:** the menu entry opens a browser tab instead. The installer
  says so when it finds none.
- `--no-desktop` skips it (and removes one an earlier run wrote); later runs remember
  that. `--desktop` brings it back. `--uninstall` removes it. An entry the installer did
  not write is never replaced or removed.

## Pinned dependencies

`constraints.txt` pins every dependency, and each user extra, to the versions the
release was tested with, for every Python from 3.10 (`tomli`, for example, only on
3.10). The installer passes it to pip or uv as a constraint, so two installs of one
checkout get the same packages, and an upgrade moves them to the new pins.
`--latest` ignores it. Maintainers re-pin with `make lock` (needs uv), or
`make lock LOCK_ARGS=--upgrade` to move every pin to the newest release; CI runs the
suite against the newest versions, so a breaking release shows there first.

## No network: a wheelhouse

On a machine with network, and the **same Python version, OS and CPU** as the target:

```bash
scripts/make_wheelhouse.sh --with-serial /path/to/wheelhouse    # or: make wheelhouse
```

It holds the Harness Manager wheel, the vendored pyverify wheel, every pinned dependency
and `constraints.txt` (about 10 MB). Copy it and the checkout across, then:

```bash
scripts/install.sh --offline /path/to/wheelhouse --with-serial
```

`--offline` never contacts the package index (pip `--no-index`, uv `--offline`). CI
proves it in a container with no network at all.

## Behind a proxy

pip and uv use the standard proxy variables. Set them before you run the installer:

```bash
export HTTPS_PROXY=http://proxy.example.com:3128
scripts/install.sh
```

Harness Manager itself talks to its service on 127.0.0.1 without the proxy, so the
variables can stay set. Cloning over SSH does not use them; where port 22 is blocked,
clone over HTTPS with a GitHub token.

## When the install stops

| What you see | What to do |
|---|---|
| `needs Python 3.10 or newer. Found only python3 (3.6.8)` | install the package it names for your distribution, or uv, and run it again |
| `could not make a venv with pip in it` | do what it names. Debian/Ubuntu: `python3.X-venv`. RHEL/Rocky/Alma 8 with `the cause: ImportError: … pyexpat … undefined symbol: XML_…`: `sudo dnf upgrade expat`. Otherwise RHEL/Rocky/Alma `python3.X-pip`, Fedora `python3-pip` |
| `the Harness Manager service did not stop` | a job is running: wait for it, or `harness-manager daemon stop --force`. A service that already exited (a zombie in a container) no longer stops the install |
| `cannot write to DIR` | fix the directory's owner, or move the install: `HARNESS_MANAGER_HOME`, `HARNESS_MANAGER_BIN_DIR` |
| `another Harness Manager install (pid N) is running` | wait for it. If none runs (a reboot), remove the `.install.lock` it names |
| `the install did not finish` | the message above it is pip's. No network: check the proxy, or use `--offline`. A pin with no wheel for a new Python: `--latest` |
| `run this without sudo` | run it as yourself: it installs into your home and needs no root |
| you pressed Ctrl-C | run it again: it resumes, and the previous version keeps working until it finishes |

## Upgrade and uninstall

- **Upgrade:** `git pull`, then run the installer again. It reinstalls Harness Manager
  and the vendored pyverify in the same venv, with your extras and menu choice, and
  never touches the state directory. A pyverify rebuilt from a newer platform commit
  keeps its version number, so the installer always reinstalls it.
- **Uninstall:** `scripts/install.sh --uninstall` (or `-Uninstall`). It stops the
  service, removes the venv, the self-updated versions, the command and the menu entry,
  and leaves the state directory: your `boards.toml`, SD backups, content store and logs.
  Delete that yourself if you want it gone.

## Self-update and the install root

Harness Manager can update itself (`harness-manager update app`). Each new version is a
new venv beside the installed one, and a pointer says which one runs. Everything lives
in the install root (`HARNESS_MANAGER_HOME`), never in the state directory:

```
~/.local/share/harness-manager/          %LOCALAPPDATA%\harness-manager\ on Windows
    install.json     what the installer installed: the venv, the version, the extras, uv
    venv/            the installer's venv; self-update never changes it
    current.json     the pointer: {"current": "0.2.0", "previous": "", "installer": {...}}
    versions/0.2.0/  a self-updated version's venv
    wheels/, reqs/   what the self-updater built those venvs from
```

- **The command follows the pointer.** It runs `versions/<current>`, with the same
  arguments and the same exit code. When `current` is `""`, it runs the installer's venv.
  The same holds on every OS: `os.execv` on Linux and macOS, and a child process on
  Windows.
- **Rollback reaches the installed version.** The installer registers its venv in the
  pointer, so `harness-manager update rollback --app` right after the first update
  returns to it.
- **Running the installer again wins.** If it installs a version at least as new as the
  self-updated one, the command runs the installed version, and the self-updated one
  stays as the rollback target. If the self-updated version is newer, it keeps running,
  and one `update rollback --app` switches to the installed version. The installer says
  which of the two happened.
- **Extras stay.** Every new version gets the extras the installer installed
  (`install.json`), as far as the release's hashed lock covers them. An extra the lock
  does not cover is left out, and the update says so.
- **uv:** the self-updater uses the uv in the installer's venv. `HARNESS_MANAGER_UV`
  names another.
- **An older install** kept its self-updated versions in `<state>/update/app/`. The first
  run of this installer moves them into the install root.
- **The way back:** `HARNESS_MANAGER_USE_INSTALLED=1 harness-manager …` runs the
  installer's venv whatever the pointer says. Use it when a self-updated version cannot
  start: `HARNESS_MANAGER_USE_INSTALLED=1 harness-manager update rollback --app`.

**Developer installs never self-update.** They never follow the pointer, and they never
change it. That covers:

- a `pip install -e` (`make venv`);
- code run from a checkout through `PYTHONPATH`;
- a venv that no installer made;
- any copy run with `HARNESS_MANAGER_NO_SELF_UPDATE=1`.

`update app` and `update rollback --app` refuse ("this is a developer install … update it
with git"). `update check` still shows what the channel has, and says this copy cannot
take it.

## Shared lab machines: the administrator's policy

Installs are per user. On a managed machine, an administrator can limit self-update for
every user with one file. Harness Manager only reads it, and a user's own settings
cannot loosen it:

| OS | File |
|---|---|
| Linux | `/etc/harness-manager/policy.toml` |
| macOS | `/Library/Application Support/harness-manager/policy.toml` |
| Windows | `%ProgramData%\harness-manager\policy.toml` |

```toml
self_update = "off"        # off | notify | stage (default stage); true = stage, false = off
channel = "stable"         # the only update channel users may use
check_interval = "12h"     # how often the service checks: s, m, h, d, or seconds; 0 = never
```

| Key | Effect |
|---|---|
| `self_update = "off"` | No app update is offered, staged or switched. `update app` refuses and names the file. Rolling back to a version already on disk still works. |
| `self_update = "notify"` | The service says an update exists, and stages it only when a user asks. |
| `self_update = "stage"` | The default: notify, stage in the background, apply on a click. |
| `channel` | Pins the channel for app and harness updates. `--channel` with another one is refused. |
| `check_interval` | The time between the service's background checks. The default is 6 h, and the minimum is 5 minutes. |

It fails closed. A file that cannot be read or parsed, or a `self_update` or `channel`
value that is not understood, turns self-update off. `update check` then shows why. An
unknown key or a bad `check_interval` is only a warning. No environment variable moves
or disables the file.

`notify`, `stage` and `check_interval` steer the service's background checks. The service
checks for app updates 60 s after it starts, then every `check_interval` (6 h by default,
with a little jitter); a failed check retries after 5 minutes, backing off to at most a day.
With `stage` it stages a new version in the background; with `notify` it only says one
exists. It never applies an update by itself. With `off`, or on a developer install, it
does not check at all.

The same file also accepts the settings tables (`docs/design/SETTINGS.md` §4):

```toml
[lock]                         # fixed for every user; no environment variable moves it
updates.channel = "stable"     # the same as channel = "stable" above
tools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"

[default]                      # this machine's starting point; a user may change it
updates.mirrors = ["/lab/mirror"]

[hubs.lab]                     # a machine hub; each user still sets their own token
transport = "ssh"
host = "mapstone-dev.ecs.soton.ac.uk"
```

`[lock]` may name the three keys above by their settings names (`updates.channel`,
`updates.check_interval`, `updates.auto`), with the same meaning. If a top-level key
disagrees with its `[lock]` entry, the top-level key is used and a warning says so.
`updates.auto` is a ceiling: a user may still choose a lower mode. Never put a token in
this file, because every user can read it. A `token` in a `[hubs.*]` table is dropped with
a warning. Only the update keys take effect today. The other locks, defaults and hubs take
effect as the Settings lanes wire each setting to them.

## Without the installer

Any venv works. Install pyverify first, by its path, so the package index never chooses
which pyverify you get:

```bash
python3 -m venv ~/hm && ~/hm/bin/pip install vendor/mps3_pyverify-*.whl
~/hm/bin/pip install --find-links vendor '.[serial]'
```

From a release directory made by `make dist`, which holds both wheels, name the files:

```bash
pip install dist/mps3_pyverify-*.whl && pip install dist/harness_manager-*.whl
```

Never install `harness-manager` or `mps3-pyverify` by name alone: neither name is ours
on PyPI, and pip prefers the highest version it can see there.

## Packaging (maintainers)

- **Version:** one source, `harness_manager.__version__` (`src/harness_manager/__init__.py`).
  `pyproject.toml` declares `dynamic = ["version"]` and setuptools reads it; the newest
  `CHANGELOG.md` heading must agree. `tests/unit/test_l5_release.py` checks it.
- **`make dist`:** builds the sdist, then the wheel from the sdist, into `dist/`; copies
  in the pyverify wheel and `constraints.txt`; writes `dist/SHA256SUMS`.
  `install.sh --from dist/harness_manager-*.whl` uses the `constraints.txt` beside it.
- **What a user gets:** `tests/integration/test_l5_packaging.py` builds both, installs
  the wheel into a clean venv, and checks the web UI's files (`index.html`, `vendor/`,
  all of them), the MPS3 board-pack entry point, `python -m harness_manager.daemon
  --help`, `harness-manager version` and `pip check`.
- **The installer:** `scripts/smoke_install.sh` (also `make smoke-install`) installs
  into a throwaway HOME, runs the command from PATH (`--version` too), checks the
  install record, uv in the venv, the self-update launcher and the Linux menu entry, starts
  `harness-manager ui --demo --no-browser`, fetches the page and its CSP header,
  upgrades in place, and uninstalls. `tests/integration/test_l5_install.py` runs it with
  pip, and with uv when uv is on PATH.
- **The installer's edges:** `tests/integration/test_q3_installer.py` runs the real
  `install.sh` from a hand-made wheelhouse, with no network, in seconds: the lock (a
  second install at once, a stale lock), Ctrl-C and resume, no write access, a system
  whose only Python is 3.6, no package index, the pins and `--latest`, remembered
  extras, the menu entry, and a clean uninstall. It also covers the self-update
  layout: the launcher following the pointer (exit codes too) and falling back, a re-run
  against an older and a newer self-updated version, `install.json` and uv, the move of
  an older install's versions, the state-dir override, and a wheel from before the
  launcher. With `pwsh` on PATH it runs `install.ps1` the same way.

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

`.github/workflows/ci.yml` runs on every push to `main` or a `ci/**` branch, and every
pull request:

| Job | Runs on | What |
|---|---|---|
| lint | ubuntu-latest | ruff, shellcheck on `scripts/*.sh`, PSScriptAnalyzer on `install.ps1`, including its syntax against Windows PowerShell 5.1 |
| tests | ubuntu-latest, Python 3.10, 3.11, 3.12 | the whole suite: unit, integration, the web UI in the runner's Chrome through Playwright, packaging, and the installer (pip and uv) |
| other-os | windows-latest, macos-latest | `tests/unit`, then the installer end to end (`install.ps1` in Windows PowerShell 5.1; `smoke_install.sh` on macOS) |

The tests job fails if the browser, packaging or installer tests skip, so a missing
Chrome cannot pass silently. pip downloads are cached per job.

`.github/workflows/install-matrix.yml` runs on the same pushes, on pull requests that
touch the installer or packaging, and every Monday (new images, new Pythons). Each job
installs [the prerequisites above](#linux-prerequisites-by-distribution) in the
distribution's own container, then runs `scripts/smoke_install.sh --with-serial` from
the checkout:

| Job | Image | What it proves |
|---|---|---|
| Rocky Linux 8 | `rockylinux/rockylinux:8` | `python3` is 3.6; the installer finds python3.12 by itself (with the updated expat) |
| Rocky Linux 9 | `rockylinux/rockylinux:9` | python3.12 from AppStream, beside a 3.9 `python3` |
| Rocky Linux 9, uv | `rockylinux/rockylinux:9` | no Python 3.10+ at all: uv downloads one, no root |
| Ubuntu 22.04, 24.04 | `ubuntu:22.04`, `ubuntu:24.04` | Python 3.10 and 3.12 with `python3-venv` |
| Debian 12 | `debian:12` | Python 3.11 |
| Fedora | `fedora:latest` | the newest Python, against the pins |
| no network | `ubuntu:24.04`, `--network none` | `make_wheelhouse.sh`, then `install.sh --offline` with no network |

The Rocky images are the ones the Rocky project publishes (`rockylinux/rockylinux`),
not the older Docker library `rockylinux` image.
