# Changelog

Each release lists what a user of Harness Manager will notice. Versions follow
`MAJOR.MINOR.PATCH`; until 1.0.0 a minor version may change the command line or the
API (docs/API.md says what changed).

## 0.1.0 (unreleased)

The first release for people outside the build team: SoC Labs staff and external MPS3
owners.

### Install
- `scripts/install.sh` (Linux, macOS) and `scripts/install.ps1` (Windows): one command
  makes a private venv, installs Harness Manager, and puts `harness-manager` on your PATH.
  It uses uv when uv is on PATH, else Python 3.10+ with venv and pip. Re-running it
  upgrades in place; `--uninstall` removes it and keeps your settings.
- Options: `--with-serial` (the Debug USB serial ports), `--with-app` (a native window
  through pywebview), `--from` a checkout, a wheel or a git URL.
- pyverify, the MPS3 shell codec, ships as a wheel in `vendor/`, with the platform commit
  it came from and its sha256 (`vendor/README.md`). It is not on PyPI.
- Tested dependency versions: `constraints.txt` pins every dependency for Python 3.10 and
  newer, and the installer uses it, so every install of a release gets the same
  packages. `--latest` takes the newest instead; `make lock` re-pins.
- Linux: **Harness Manager** in the desktop's application menu, with an
  **Open with Demo Boards** action (`--no-desktop` skips it). It is written on a server
  with no display too, for a later remote desktop such as ThinLinc.
- No network: `scripts/make_wheelhouse.sh` (or `make wheelhouse`) collects every wheel,
  and `install.sh --offline DIR` installs from it without the package index.
- The installer finds a Python 3.10+ beside an older `python3` (RHEL 8's 3.6, Rocky 9's
  3.9), and when there is none it lists what it found and prints the package to install
  for your distribution, or the uv one-liner. When a Python cannot make a venv, it
  shows the real cause, which venv hides, and names the fix: `python3.X-venv`, or on
  RHEL 8 `sudo dnf upgrade expat`, because python3.12 needs a newer expat than an
  un-updated system has.
- The installer is safe to run twice at once (the second stops and names the first),
  safe to interrupt (a re-run resumes), checks it can write before it starts, refuses
  `sudo`, keeps your extras and menu choice across upgrades, explains a missing network
  or proxy, and prints the full path of the command when `~/.local/bin` is not on PATH.
- `harness-manager daemon stop`, and so every upgrade, no longer fails inside a
  container. There, a stopped service stays a zombie, which still looked alive. It also
  never signals a process that has reused the service's old pid: it checks the pid is
  this state dir's harness-manager-daemon first.
- daemon.log is capped: over 8 MiB it moves to `daemon.log.1` (three kept), at
  `daemon start` and once a minute while the service runs.
- `daemon start` on a state directory it cannot write says so, with the next step
  (exit 6), instead of "internal error: PermissionError".
- `harness-manager app` with `--with-app` on Linux no longer prints two pywebview
  tracebacks: without GTK or Qt bindings it goes straight to the Chrome app window.
- CI installs from a checkout on Rocky Linux 8 and 9, Ubuntu 22.04 and 24.04, Debian 12
  and Fedora, with only each distribution's prerequisites, and once with no network.

### The app and the command
- `harness-manager app`: the web UI in its own window (pywebview, or a Chrome, Edge or
  Chromium app window). `harness-manager ui`: the same page in a browser tab.
  `--demo` on either shows scripted boards with no hardware.
- The app window starts without the desktop's D-Bus session bus, so it is no longer
  blank on ThinLinc. `HARNESS_MANAGER_APP_KEEP_DBUS=1` keeps the bus.
- With no display (an SSH session), `app` and `ui` print the URL and the `ssh -L`
  command to reach it.
- A per-user background service (harness-manager-daemon) owns the board sessions, so
  the command and the app share one board. It listens on 127.0.0.1 only, with a token.
- The command: `probe`, `info`, `attach`/`detach`, `telemetry`, `overlays`, `program`,
  `restore`, `console`, `debug`, `reset`, `clock`, `lab`, `mcc`, `sd`, `update`,
  `daemon`, `ui`, `app`, `help`. Every verb has `--json` and `--tsv` output and
  documented exit codes (`harness-manager help --tabs`).

### The MPS3 board pack
- Identify the harness and its health (idle, busy, wedged, offline, service down,
  rescue), with what to do for each.
- The capability view: every feature the board has, and for each one it lacks, what it
  needs ("needs the Debug USB cable", "needs harness firmware with 'stats'").
- Program the DUT partition with a preflight check against the shell, then confirm the
  load. Restore the baseline design.
- Programming a board that runs the Linux harness always uses TCP and waits up to 30 s
  for each part of the push. When you swap away from a design with a debug port
  (nanosoc), the harness takes the new design only after it has cleared the old one,
  and over TFTP it rejected the new design (found on the board, 25 Sep).
- After a failed push, the harness turns new connections away for up to 30 s while it
  finishes that swap. The board now shows as busy, with that reason, instead of offline.
- DUT consoles (UART0, UART1, SWO) over Ethernet; the MCC and the FPGA UARTs over the
  Debug USB.
- Each console can also be a terminal device for `screen`
  (`/tmp/harness-manager-$USER/<board>/<console>`, `harness-manager pty`), shown in the
  app and in `screen` at the same time (Linux and macOS).
- Console rates: `harness-manager baud` and the app show each console's rate. Serial
  consoles change rate; Ethernet consoles report the loaded design's fixed rate (76800
  on nanosoc) and say why they cannot change it.
- A debug server for the DUT CPU (OpenOCD), for gdb and Arm DS.
- Reset the DUT, set the DUT clock, read temperatures and oscillators.
- The board controller (MCC) over the Debug USB: temperatures, oscillators, a reboot
  that proves the board came back, and allowlisted commands (destructive ones are
  refused).
- The configuration SD: backup, install and restore. A backup comes first, `.ebf` files
  are never written, and an interrupted install can be recovered.
- Board power from a networked plug (Shelly, Tasmota, NETIO) or an INA260, set in
  `boards.toml`: `harness-manager power show`, and a cold `power cycle`.
- Signed updates of the harness and the app (`harness-manager update`). The channel is
  not live yet: it refuses every release until the release keys are made.
- Harness versions: `harness-manager harness list|show|fetch|install|pin|unpin|history|
  rollback|mirror` (and the `/harness` API) lists every release of the board's harness
  catalogue with a verdict for the board (fits, re-key, needs Debug USB or hub,
  incompatible) and what it would change, installs a chosen version, pins a board to a
  release, keeps each board's last 20 installs, and rolls back to the release the last
  install replaced. A board behind a hub is installed on only by the lease holder.
  `update` still works as before.
- Boards behind a lab hub: `via = "ssh:HOST"` in `boards.toml`, or `--via ssh:HOST`,
  reaches the board through one supervised SSH tunnel (consoles, programming, debug).
  The MCC and FPGA UART lanes work over the hub's serial shares. `harness-manager lease`
  and `share` manage the hub lease and shares (there is no `share stop`: it would stop
  every share on the board). The lease is heartbeated while the board is open.
- DUT console input is paced (20 ms a byte on UART0/UART1), because the nanoSoC UART has
  no receive FIFO: a paste no longer arrives garbled.

### The app's pages
- A simpler Overview: four tiles (Design, Consoles, Debug, Board), a "Needs attention"
  line only when something is wrong, and the details folded away.
- Consoles: "Attach with screen" gives the command to copy, the rate and why it is
  fixed, a rate selector where it can change, and Export to TCP.
- Power, Update, Clocks and SD card pages; a lease and tunnel chip for boards behind a
  hub.

### Known limits
- The board has a fixed address, 192.168.10.101, and there is no network discovery yet.
- Windows and macOS run the unit tests and the installer in CI; they have not been used
  with a real board. `install.ps1` does not yet use `constraints.txt`, the wheelhouse or
  a Start-menu entry.
