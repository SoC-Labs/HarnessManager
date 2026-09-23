# SoC Labs Harness Manager

Harness Manager is a board manager for nanoSoC DUT harnesses on Arm MPS3 FPGA boards.
It finds a board, programs its DUT partition, opens the DUT's consoles, runs a debug
server for the DUT CPU, resets it, sets its clock, reads temperatures, and looks after
the board controller (MCC) and its configuration SD card.

You get a command, `harness-manager`, and an app window. Both drive the same board
session, so you can use them at the same time.

Version 0.1.0. Linux first; Windows and macOS are supported too. This repository is
private, and its licence is still to be decided.

## Install

You need Python 3.10 or newer, or [uv](https://docs.astral.sh/uv/) (uv fetches a
Python for you), and git. The repository is private: ask SoC Labs for access, and add
an SSH key to your GitHub account. Then clone it and run the installer.

Linux and macOS:

```bash
git clone git@github.com:SoC-Labs/HarnessManager.git && HarnessManager/scripts/install.sh
```

Windows (PowerShell):

```powershell
git clone git@github.com:SoC-Labs/HarnessManager.git; powershell -ExecutionPolicy Bypass -File HarnessManager\scripts\install.ps1
```

The installer makes a private venv, installs Harness Manager into it, and puts the
`harness-manager` command on your PATH. Nothing needs root or Administrator.

| Option (`install.sh` / `install.ps1`) | What it adds |
|---|---|
| `--with-serial` / `-WithSerial` | pyserial, for the board's Debug USB serial ports. Take it if you own an MPS3. |
| `--with-app` / `-WithApp` | pywebview, for a native app window. Without it the app opens a Chrome, Edge or Chromium window. |
| `--from PATH-or-URL` / `-From` | install from another checkout, a wheel, or a git URL |

To upgrade, `git pull`, then run the installer again. To remove Harness Manager, run it
with `--uninstall` (`-Uninstall`). Your settings and SD backups stay.

More detail, including where everything goes: [docs/INSTALL.md](docs/INSTALL.md).

## First run

```bash
harness-manager app --demo
```

This opens the app with scripted demo boards. It needs no hardware, so you can look
around safely. Close the window when you are done. `harness-manager daemon stop --demo`
stops the demo service.

## Connect to a board

### Directly over Ethernet (you own the board)

The harness answers on a fixed address, `192.168.10.101`. Plug the board into your PC,
and give that Ethernet port an address on the same network, for example `192.168.10.1`
with netmask `255.255.255.0`. Then:

```bash
harness-manager info 192.168.10.101     # identity, health, and what the board can do
harness-manager app                     # the app; add the board by its address
```

A new board first needs the harness installed onto its SD card, once, over the Debug
USB cable. After that, daily use needs only the Ethernet cable. The
[user guide](docs/USER_GUIDE.md) walks through both.

### Through a lab hub (the SoC Labs lab)

Lab boards sit behind a hub machine. Harness Manager reaches them through an SSH
tunnel to the hub, so you need SSH access to the hub. Name the hub once, in
`~/.config/harness-manager/boards.toml`:

```toml
[boards."mps3@192.168.10.101:6900"]
via = "ssh:srv03335"
```

or give it for one command: `harness-manager info 192.168.10.101 --via ssh:srv03335`.
A hub board is shared, so Harness Manager takes a lease on it for you and keeps it
while the board is open. The app shows the lease holder and when it ends.

## Consoles

Open a console from the app: the **Consoles** tile has an **Open** button for each one.

The same console is also a terminal device, so you can attach `screen` to it while the
app shows it:

```bash
screen /tmp/harness-manager-$USER/<board>/uart0
```

The app shows the exact `screen` command, with a button to copy it. From a terminal,
`harness-manager pty TARGET uart0` makes the same device, prints the command, and keeps
the device until Ctrl-C. One `screen` at a time per console; the app can show it too.

The baud rate:
- **Ethernet consoles** run at the rate the loaded design was built with: uart0 is
  76800 on nanosoc, and swo is 2000000. The app shows it. `screen` needs no baud
  argument for them.
- **Serial consoles** on the Debug USB (the MCC, the FPGA UARTs) can change rate: in the
  app, or with `harness-manager baud TARGET NAME RATE`. Their `screen` command carries
  the rate, because `screen` alone sets 9600.

On Windows there is no `screen`. Use the console in the app, or
`harness-manager console TARGET uart0 --export 0` and a raw TCP terminal such as PuTTY.

## Everyday commands

| Command | What it does |
|---|---|
| `harness-manager probe` | look for boards (the default address and USB) |
| `harness-manager info TARGET` | identity, health, and every capability, with the reason when one is missing |
| `harness-manager program TARGET nanosoc` | program the DUT partition (checks first, asks, then verifies) |
| `harness-manager console TARGET uart0` | the DUT's UART0 in this terminal (Ctrl-] exits) |
| `harness-manager debug up TARGET` | OpenOCD for the loaded design; connect gdb to the port it prints |
| `harness-manager reset TARGET` | reset the DUT |
| `harness-manager restore TARGET` | back to the baseline design |
| `harness-manager pty TARGET uart0` | a console as a terminal device, for `screen` |
| `harness-manager power cycle TARGET` | switch the board off and on (needs a networked plug in `boards.toml`) |

`TARGET` is the board's address, for example `192.168.10.101`. Add `--json` to any verb
for one JSON object on stdout. `harness-manager help --tabs` prints the full help,
including every exit code.

## Troubleshooting

**The app window is blank (ThinLinc and other remote desktops).** Fixed in 0.1.0: the
app window now starts without the desktop's D-Bus session bus, which was the cause. If
something else on your desktop needs that bus, set `HARNESS_MANAGER_APP_KEEP_DBUS=1`.
`harness-manager ui` opens the same page in a normal browser tab.

**No display (an SSH session).** `harness-manager app` and `harness-manager ui` print
the URL and the `ssh -L` command that forwards it. Run that on your own machine, then
open the URL there.

**`harness-manager: command not found`.** `~/.local/bin` is not on your PATH. The
installer printed the line to add to your shell's startup file. On Windows, open a new
terminal after the first install.

**A feature is greyed out.** The app, and `harness-manager info`, say what it needs,
for example "needs the Debug USB cable". See the user guide.

**Anything else.** `harness-manager daemon status` shows the background service, and
its log is `~/.config/harness-manager/daemon.log`. `harness-manager daemon stop` stops
it; the next command starts it again.

## Where things are

| What | Linux and macOS | Windows |
|---|---|---|
| The command | `~/.local/bin/harness-manager` | `%LOCALAPPDATA%\harness-manager\bin` |
| The program (venv) | `~/.local/share/harness-manager/venv` | `%LOCALAPPDATA%\harness-manager\venv` |
| Settings, `boards.toml`, SD backups, logs | `~/.config/harness-manager` | `%USERPROFILE%\.config\harness-manager` |

## For developers

```bash
make venv     # .venv with harness-manager and pyverify, editable
make check    # lint and the whole test suite, against a virtual MPS3
make dist     # sdist and wheel in dist/, with the pyverify wheel
```

- How it is built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- The local service API: [docs/API.md](docs/API.md)
- Contracts between teams: [docs/CONTRACTS.md](docs/CONTRACTS.md)
- The team plan: [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md)
- Installing, packaging, CI and the vendored pyverify: [docs/INSTALL.md](docs/INSTALL.md)
- What changed: [CHANGELOG.md](CHANGELOG.md)

## Licence

Private to SoC Labs. The licence is still to be decided.
