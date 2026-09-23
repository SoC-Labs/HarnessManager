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
- DUT consoles (UART0, UART1, SWO) over Ethernet; the MCC and the FPGA UARTs over the
  Debug USB.
- A debug server for the DUT CPU (OpenOCD), for gdb and Arm DS.
- Reset the DUT, set the DUT clock, read temperatures and oscillators.
- The board controller (MCC) over the Debug USB: temperatures, oscillators, a reboot
  that proves the board came back, and allowlisted commands (destructive ones are
  refused).
- The configuration SD: backup, install and restore. A backup comes first, `.ebf` files
  are never written, and an interrupted install can be recovered.
- Board power from a networked plug (Shelly, Tasmota, NETIO) or an INA260, set in
  `boards.toml`.
- Signed updates of the harness and the app (`harness-manager update`). The channel is
  not live yet: it refuses every release until the release keys are made.

### Known limits
- The board has a fixed address, 192.168.10.101, and there is no network discovery yet.
- Windows and macOS run the unit tests and the installer in CI; they have not been used
  with a real board.
