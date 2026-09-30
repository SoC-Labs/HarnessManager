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

You need git and Python 3.10 or newer (with `venv`), or [uv](https://docs.astral.sh/uv/),
which fetches a Python for you. On a fresh system:

| Linux | Install first |
|---|---|
| Ubuntu 22.04+, Debian 12 | `sudo apt install python3-venv git` |
| Rocky, RHEL, Alma 8 or 9 | `sudo dnf install python3.12 expat git` (their `python3` is too old; RHEL 8's python3.12 needs the updated expat) |
| Fedora | `sudo dnf install python3 git` |

[docs/INSTALL.md](docs/INSTALL.md#linux-prerequisites-by-distribution) has the exact
lines CI tests on each. The repository is private: ask SoC Labs for access, and add an
SSH key to your GitHub account. Then clone it and run the installer.

Linux and macOS:

```bash
git clone git@github.com:SoC-Labs/HarnessManager.git && HarnessManager/scripts/install.sh
```

Windows (PowerShell):

```powershell
git clone git@github.com:SoC-Labs/HarnessManager.git; powershell -ExecutionPolicy Bypass -File HarnessManager\scripts\install.ps1
```

The installer makes a private venv, installs Harness Manager into it at tested
dependency versions, puts the `harness-manager` command on your PATH, and on Linux adds
**Harness Manager** to your desktop's application menu. It takes about 20 seconds.
Nothing needs root or Administrator.

| Option (`install.sh` / `install.ps1`) | What it adds |
|---|---|
| `--with-serial` / `-WithSerial` | pyserial, for the board's Debug USB serial ports. Take it if you own an MPS3. |
| `--with-app` / `-WithApp` | pywebview, for a native app window on Windows and macOS. Linux uses a Chrome or Chromium app window. |
| `--from PATH-or-URL` / `-From` | install from another checkout, a wheel, or a git URL |
| `--offline DIR` | install with no network, from a wheelhouse ([docs/INSTALL.md](docs/INSTALL.md#no-network-a-wheelhouse)) |
| `--no-desktop` | no application menu entry (Linux) |

To upgrade, `git pull`, then run the installer again. It keeps your settings, and
`install.sh` also keeps the options you chose. To remove Harness Manager, run it with
`--uninstall` (`-Uninstall`). Your settings and SD backups stay.

More detail, including proxies and where everything goes: [docs/INSTALL.md](docs/INSTALL.md).

## First run

```bash
harness-manager app --demo
```

This opens the app with scripted demo boards. It needs no hardware, so you can look
around safely. Close the window when you are done. `harness-manager daemon stop --demo`
stops the demo service. On Linux you can also start it from the application menu:
**Harness Manager**, or its **Open with Demo Boards** action.

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

Lab boards sit behind a hub machine (fpgahub). You need an SSH account on the hub, or an
fpgahub token. Add the hub once as a named hub, test it, then add the board it offers:

```bash
harness-manager hub add lab --ssh mapstone-dev.ecs.soton.ac.uk
harness-manager hub test lab                        # never takes a lease
harness-manager hub targets lab --add mps3_01_pl    # writes the board into boards.toml
```

A hub board is shared, so take its lease before you change anything on it:
`harness-manager lease acquire 192.168.10.101`, or **Acquire lease** in the app. Harness
Manager never takes a lease for you. Once it is yours, the service renews it while the
board is open, and the app shows the holder and when it ends.

`--via ssh:HOST` on one command, or `via = "ssh:HOST"` in `boards.toml`, gives only the
SSH tunnel: there is no hub, so there is no lease. The
[user guide](docs/USER_GUIDE.md#32-a-lab-board-behind-a-hub) has the token form and the
details.

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
installer printed the line to add for your shell, and the full path to use until then
(`~/.local/bin/harness-manager`). With bash, add it to `~/.bashrc` and to `~/.bash_profile`
(or `~/.profile`, whichever exists): ssh and `bash -l` start a login shell, which never
reads `~/.bashrc`. Every shell's line and file:
[docs/INSTALL.md](docs/INSTALL.md#when-harness-manager-is-not-on-path). On Windows, open a
new terminal after the first install.

**The installer stopped.** It says why, and what to install or change: a Python that is
too old, no `python3-venv`, no network or a proxy, no write access. Running it again is
always safe. [docs/INSTALL.md](docs/INSTALL.md#when-the-install-stops) lists each case.

**A feature is greyed out.** The app, and `harness-manager info`, say what it needs,
for example "needs the Debug USB cable". See the user guide.

**Debug says OpenOCD is missing.** Debugging needs OpenOCD (0.12 or later) built with the
remote_bitbang adapter, on your PATH or in `tools.openocd`. Some builds lack it (check with
`openocd -c "adapter list" -c shutdown`); xPack OpenOCD 0.12 has it.
The MPS3 target configs ship with Harness Manager.

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
