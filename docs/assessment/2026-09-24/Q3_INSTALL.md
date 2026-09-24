# Q3 Install assessment (2026-09-24)

Lane Q3 looked at installing Harness Manager and using it for the first time on Linux,
for people outside the build team. The work is on branch `team/q3-install`, from `main`
at `64eb716`.

## Verdict

**Before this lane:** the happy path was already good: one command, about 20 s, a working
`harness-manager app --demo`. The edges were rough:

- On a system whose only Python is 3.6, the installer failed with a generic message.
- With no network or behind a proxy, users saw pip's raw error.
- Two installs at once could corrupt the venv.
- `--with-app` on Linux printed two tracebacks.
- The app was not in the desktop menu.
- Dependencies floated, so no two installs were guaranteed to match.
- Only Ubuntu was ever tested.

**After this lane:**

- Every edge case gives a clear message, and a second run fixes it.
- Upgrades work inside containers: CI's first run found that `daemon stop` waited on a
  zombie.
- A reused pid is never signalled (lane Q1's finding).
- The desktop menu has a launcher.
- The installer uses pinned dependency versions (the tested ones).
- An offline install works.
- CI installs on six distribution images (seven jobs), plus once with no network.

### First-time user

It takes 3 steps and 22 s from `git clone` to a live app window. Measured on srv03335
under load average 25, with a fresh HOME, an empty pip cache and Python 3.12:

| Step | Time |
|---|---|
| `git clone …` (local clone; GitHub adds network time) | 0.4 s |
| `HarnessManager/scripts/install.sh` | 19.2 s |
| `harness-manager app --demo` (the command returns) | 1.1 s |
| The page is live in the Chrome app window (its WebSocket connects) | 1.3 s |

With uv the install takes 10.5 s cold (uv downloads Python itself) and 4 s warm.

## Local matrix (srv03335, RHEL 8.10; every run used a throwaway HOME)

There is no docker or podman on this host, so other distributions run in CI (next
section).

| Scenario | Result | Time |
|---|---|---|
| From a checkout: pip, Python 3.12 picked by itself | PASS | 17–20 s |
| From a checkout: `--python python3.11 --with-serial` | PASS, `pip check` clean | 23 s |
| From a built wheel (`make dist`, then `--from dist/*.whl`) | PASS | 20 s |
| From a git URL with `--ref` and `--no-desktop` | PASS, no menu entry, choice remembered | 20 s |
| uv installed into the throwaway HOME; only Python 3.6 on PATH | PASS: uv fetched CPython 3.12.14 | 10.5 s |
| No uv; only Python 3.6 on PATH | Clear refusal: lists `python3 (3.6.8)`, prints `sudo dnf install python3.12` and the uv one-liner (was a generic message) | <1 s |
| `harness-manager --version` / `version` | `Harness Manager 0.1.0` / `0.1.0` | |
| `app --demo` over SSH (no display) | Prints the URL and the `ssh -L` command | 1.2 s |
| `app --demo` on a private Xvfb | Chrome app window; title "Harness Manager"; `WM_CLASS` class `harness-manager`; page loaded | 1–2 s |
| The menu entry, launched with `gtk-launch harness-manager` (session bus set to a dead address) | Chrome app window, page loaded | |
| `--with-app` on Linux | Was two pywebview tracebacks (GTK, Qt) before Chrome; now Chrome with no output, and the installer explains why | |
| Re-run (idempotent) | Upgrades in place; `[serial]` kept with no flag; `boards.toml` untouched | 5–15 s |
| Uninstall | Removes the command, venv, menu entry, icon, `install.conf` and install root. Keeps `~/.config/harness-manager`. Leaves only empty XDG directories | <1 s |
| `~/.local/bin` not on PATH | Prints the line for your shell. **Next** lines use the full path (they used to be commands that failed) | |
| No package index (nothing answering) | pip's error, then the installer's advice: proxy, `--offline`, `--latest`, "running it again is safe" | ~25 s of pip retries |
| `--offline` from a wheelhouse, with the index and every proxy variable pointed at nothing | PASS, including the demo UI | 12 s to build the wheelhouse |
| Behind a proxy: `HTTPS_PROXY` set to a local CONNECT proxy | PASS: 3 CONNECTs to pypi.org and 3 to files.pythonhosted.org. The CLI and service are unaffected by the proxy variables | 17 s |
| Bin directory not writable | Refused in 0.05 s, naming the directory and `HARNESS_MANAGER_BIN_DIR`. Was a raw `mkdir` error, after the 20 s install | 0.05 s |
| Install root not writable | Refused at once, naming `HARNESS_MANAGER_HOME` | 0.05 s |
| Two installs at once | The second is refused in under 1 s, naming the first's pid; the first finishes. There was no lock before | |
| Ctrl-C during pip | Exit 130, "run it again", lock released; the re-run resumes and completes | |
| `smoke_install.sh`, pip and uv | SMOKE PASS in 29 s (pip) and 13 s (uv) | |

## What the CI distribution matrix will prove

This is the new `.github/workflows/install-matrix.yml`. It runs on pushes to `main` and
`ci/**`, on pull requests that touch the installer, and weekly.

Each job runs `scripts/smoke_install.sh --with-serial` inside the distribution's
container. Before that, the job installs only that distribution's prerequisites. These
are the lines in docs/INSTALL.md, and `test_q3_packaging.py` keeps the docs and the
workflow identical. Each job then checks:

- the install;
- `harness-manager --version` and `version`;
- the Linux menu entry;
- `ui --demo --no-browser`;
- the page, with its CSP header and `/api/v1/health`;
- an upgrade in place;
- the uninstall.

| Job | Prerequisites | Expected |
|---|---|---|
| Rocky Linux 8 | `dnf install -y python3 python3.12 python3.12-pip git tar` | PASS: the installer skips the 3.6 `python3` and picks python3.12 |
| Rocky Linux 9 | `dnf install -y python3.12 git-core tar` | PASS on 3.12, beside a 3.9 `python3` |
| Rocky Linux 9, uv | `dnf install -y git-core tar`, then the uv one-liner | PASS: uv downloads CPython 3.12 |
| Ubuntu 22.04 | `apt-get install … python3 python3-venv git curl ca-certificates` | PASS on 3.10 (tomli, exceptiongroup, websockets 16 pins) |
| Ubuntu 24.04 | the same | PASS on 3.12 |
| Debian 12 | the same | PASS on 3.11 |
| Fedora latest | `dnf install -y python3 git-core tar` | PASS on the newest Python (3.14), against the pins |
| No network | Build `ubuntu:24.04` with the prerequisites, run `make_wheelhouse.sh` with network, then `smoke_install.sh --offline` under `--network none` | PASS, and a probe proves PyPI is unreachable |

Risks, and what to do if one of them fails:

- The Rocky images are the Rocky project's `rockylinux/rockylinux:8` and `:9`. If they
  cannot be pulled, use `rockylinux:8` and `rockylinux:9` instead.
- On Fedora, a pinned wheel may be missing for its newest Python. The log would show a
  source build of pydantic-core. If so, run `make lock LOCK_ARGS=--upgrade` and push
  again.
- The Docker Hub pull rate limit applies to anonymous pulls.

Each job takes about 2 to 3 minutes, and they run in parallel.

### CI round 1 (run 35993990472): what it caught

The first run of the matrix failed 7 of its 8 jobs. Only the offline job passed. There
were three causes, all fixed on this branch (F14 to F16), and lane Q1 found a fourth
problem in the same code (F17):

- **Ubuntu 22.04 and 24.04, Debian 12, Fedora, Rocky 9:** the upgrade failed. Its
  `daemon stop --demo` exited 6 with "did not stop".
- **Rocky 8:** python3.12 could not make a venv, and the installer told the user to
  `apt install`.
- **Rocky 9 with uv:** the job never got past `actions/checkout`, which found no `git`
  or `tar`.

I reproduced the first cause on srv03335 in a user+PID namespace whose PID 1 is
`tail -f /dev/null`, which is how GitHub runs a container job. There, the smoke from the
previous commit failed exactly as in CI. The fixed tree passes (SMOKE PASS in 32 s). An
old install upgraded by the new `install.sh` also passes; the installer notes that the
service "had already exited".

## Findings and fixes

| # | Finding | Fix | Test |
|---|---|---|---|
| F1 | When the only Python is too old (RHEL 8's 3.6), the message named no version and no package. Rocky 9 (3.9) is the same | The installer lists what it found and prints the package line for the distribution, read from `/etc/os-release`: dnf, apt, zypper or pacman. It also prints the uv one-liner and `--python`. python3.13 and python3.14 were added to the search | `test_no_new_enough_python_says_what_to_install` |
| F2 | There was no lock. Two installs at once both ran `rm -rf` and made the venv | A `mkdir` lock with the holder's pid. A live holder is refused, and the message says how to clear the lock. A stale lock (killed install, or a reboot) is taken over | `test_second_install_at_once_is_refused`; the stale lock in `test_lifecycle_…` |
| F3 | An interrupted install left no message | INT, TERM and HUP print "run it again", exit 130 or 143, and release the lock. The re-run resumes | `test_interrupted_install_resumes` |
| F4 | No write access failed late with a raw error (the bin dir after the whole install) | Both directories are checked first, and the message names the variable that moves each one. `mktemp` failure names `TMPDIR`. `sudo` is refused, because it would install for root | `test_no_write_access_is_reported_before_any_work` |
| F5 | With no network or a broken proxy, the only output was pip's error (setuptools build deps) | Any failed pip or uv step prints the next move: `HTTPS_PROXY`, `--offline DIR`, `--latest`, "running this again is safe" | `test_no_index_explains_proxy_and_offline` |
| F6 | There was no offline install; only pyverify was vendored | `scripts/make_wheelhouse.sh` (`make wheelhouse`) and `install.sh --offline DIR`, which never contacts the index | `test_lifecycle_…` (every run is offline); CI `offline` job |
| F7 | Dependencies floated, with lower bounds only; no two installs were guaranteed to match | `constraints.txt`: a universal uv lock with 36 pins for Python 3.10+ on every OS, including the user extras. The installer uses it as a constraint, and an upgrade converges to new pins. `--latest` rebuilds with the newest versions. `make lock` re-pins. `make dist` ships the lock beside the wheel | `test_pins_hold_unless_latest`, `test_constraints_*`, `test_each_pin_satisfies_pyproject` |
| F8 | The app was not in the desktop menu | `packaging/linux/harness-manager.desktop` and an icon (the favicon), per user. It has a "Demo Boards" action, `StartupWMClass=harness-manager` (matching Chrome's `--class`) and `StartupNotify=false`. `--no-desktop` skips it and `--desktop` restores it; the choice is remembered. Uninstall removes it, and an entry the installer did not write is never touched. It is written on headless servers too, for a later ThinLinc or VNC session | `test_lifecycle_…`, `test_desktop_*`, `test_a_menu_entry_we_did_not_write_is_left_alone`; smoke checks it on every distribution |
| F9 | `--with-app` on Linux installs pywebview, but a venv has no GTK or Qt bindings. Every `app` printed two tracebacks, then fell back to Chrome | Product code, marked in `web/window.py`: on Linux, pywebview is skipped quietly when neither `gi` nor `qtpy` can be imported. The installer and docs say that Linux uses the Chrome app window | `test_q3_window.py` |
| F10 | Extras were kept only because pip never removes packages; a rebuilt venv lost them | `install.conf` records the extras and the menu choice; re-runs merge them | `test_lifecycle_…` |
| F11 | When `~/.local/bin` was not on PATH, the printed **Next** commands failed | The installer prints the line for your shell (bash, zsh, fish or sh) and gives the full path in **Next** until the PATH is fixed | `test_lifecycle_…` |
| F12 | `smoke_install.sh` could create consoles in the live `/tmp/harness-manager-$USER`, and its curl would use `http_proxy` for 127.0.0.1 | It sets its own `HARNESS_MANAGER_PTY_DIR`. It uses `curl --noproxy '*'`. It also checks `--version`, the menu entry and a complete uninstall, and prints timings | CI and `test_l5_install` |
| F14 | **Found by CI (product bug). Lane Q2 found the same zombie from the app window's side and fixed `pid_alive` in the same way; main keeps Q2's version, and these tests cover both.** In a container whose PID 1 never reaps orphans (`docker run` with no init, and GitHub's container jobs), a stopped daemon stays a zombie. `os.kill(pid, 0)` still succeeds on a zombie. So `daemon stop` waited 15 s and failed with exit 6, and so did every upgrade | Product code, marked: `core/session.py` `pid_alive` treats /proc state Z or X as dead. This covers `daemon stop`, `daemon status` and stale board locks. `install.sh` also carries on when an older installed CLI says the stop failed but the service is gone (no `daemon.json`, no process, or a zombie). A service that really runs still stops the install | `test_q3_daemon_zombie.py`: 3 of its 4 tests fail without the fix. `test_upgrade_carries_on_when_the_service_already_exited` and its twin |
| F15 | **Found by CI.** On Rocky and RHEL 8, python3.12 cannot make a venv without `python3.12-pip`. The installer's hint said `apt install python3-venv` | The prerequisites (workflow, INSTALL.md, README) include `python3.12-pip`. The venv hint names the package for the distribution: `python3.X-venv` (Debian, Ubuntu), `python3.X-pip` (RHEL family), `python3-pip` (Fedora) or zypper (SUSE). It still prints the tool's own error | `test_a_python_that_cannot_make_a_venv_names_the_package` (Rocky, Fedora, Ubuntu) |
| F16 | **Found by CI.** In a container job, `$GITHUB_PATH` replaced the image's PATH for the steps after it, so `actions/checkout` found no `git` and no `tar` | uv's bin directory is exported in the smoke step instead of through `$GITHUB_PATH` | the next CI run |
| F17 | **From lane Q1.** `daemon stop` sent SIGTERM to whatever process held the pid in `daemon.json`. If the pid had been reused, an upgrade could kill an unrelated process | Product code, marked: `control.is_our_daemon`. The pid counts as ours only if `/health` answers with it, or if its command line is `-m harness_manager.daemon --state-dir <this dir>` (or `daemon start --foreground`). This is checked before the shutdown request and again right before any signal. A foreign pid means the daemon is gone: `daemon.json` is removed ("stale-removed") and nothing is signalled. Where the command line cannot be read (Windows), a hung daemon is not signalled, and the message says to stop it by hand | `test_q3_daemon_pid_reuse.py`: a reused pid and another state dir's daemon are never signalled; the twin checks that our own hung daemon still gets SIGTERM |
| F18 | **From lane Q2's soak.** daemon.log grew 0.5 to 0.6 MB an hour, and nothing trimmed it | Product code, marked: `daemon/logfile.py`. `daemon start` rotates daemon.log over 8 MiB to `.1` to `.3`, and the oldest drops. The running daemon checks once a minute: it renames its own log and points its stdout and stderr at a fresh one (`dup2`), so every writer follows. A `--foreground` daemon, whose stdout is not the file, is left alone. On Windows an open file cannot be renamed, so the next start rotates instead | `test_q3_daemon_log.py`: shift and drop, twin under the cap, the start rotates, the running daemon rotates both fds, twin foreground |
| F19 | **From lane Q2.** `daemon start` on a read-only state dir printed "internal error: PermissionError" (exit 1) | `control._spawn`: an OSError on the state dir or the log is now "cannot start harness-manager-daemon: cannot write …: it is not writable (or the disk is full)", with the next step, exit 6. It uses Q2's `storage_error` | `test_daemon_start_on_a_read_only_state_dir_is_a_message`; the CLI returns 6, not "internal error". Both fail on the old code |
| F13 | Only Ubuntu was tested; the documented prerequisites were untested | The distribution matrix, above. INSTALL.md lists the exact tested line for each distribution, and README gives a short table | `test_install_doc_gives_what_ci_proves` |

### Checked and found correct

- `tomli` has the right marker for 3.10 (`python_version < '3.11'`), and
  `power/config.py` falls back to it.
- pyverify is installed by path first with `--force-reinstall --no-deps`. `--find-links`
  is set, the upgrade strategy is only-if-needed, and it is excluded from the pins. The
  index never picks it.
- Every module of `harness_manager` and `harness_manager_mps3` imports in a clean
  installed venv. `pip check` is clean.
- No system library is needed. Every dependency is a manylinux wheel. The oldest one,
  cryptography, is `manylinux_2_28`, so it needs glibc 2.28: RHEL 8+, Debian 10+,
  Ubuntu 20.04+.
- The CLI and the service ignore proxy variables for 127.0.0.1. `http.client` goes
  direct, and the websockets client passes `proxy=None`.

## What remains

1. **Windows:** `install.ps1` does not use `constraints.txt` or `--offline`, and adds
   no Start-menu shortcut. It is next when Windows is in scope.
2. **Hash-checked installs:**
   - The pins are versions only. `pip --require-hashes` would also check the downloads.
   - The app self-update (`services/update/app.py`) expects a hashed lock asset in each
     release; none exists yet.
   - Both can come from `uv pip compile --generate-hashes`, with pyverify pinned by path
     and hash.
3. **A no-git install path for external owners.** The private repo, SSH key and git are
   the biggest first-use hurdle. A release asset would remove it: the wheel, the
   pyverify wheel, `constraints.txt` and `install.sh`, with `--from` the wheel.
4. **Menu-launched errors are invisible.** From the menu, a failure has no terminal to
   print to, for example no browser at all. A desktop notification is product work.
5. **Not in CI:**
   - aarch64 Linux: an `ubuntu-24.04-arm` runner job;
   - openSUSE and Arch: the installer prints their hints, but CI does not test them.
6. **USER_GUIDE (outside this lane's files):**
   - The `dialout` note says "Ubuntu and Debian", but RHEL and Fedora use the same group.
   - A headless server has no automount for the MCC's USB drive: the user needs
     `udisksctl mount -b /dev/sdX1` or sudo.
