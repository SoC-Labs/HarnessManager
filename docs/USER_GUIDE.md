# User guide: Harness Manager on your own MPS3

This guide is for people who own an Arm MPS3 (V2M-MPS3) and want to use it for nanoSoC
work with no lab hub: one PC, the board, a Debug USB cable and an Ethernet cable.

You use the Debug USB cable **once**, to back up the board's configuration SD card and
install the SoC Labs harness onto it. After that, daily work needs only Ethernet.

Contents:
1. [What you need](#1-what-you-need)
2. [Install Harness Manager](#2-install-harness-manager)
3. [First install on the board (Debug USB)](#3-first-install-on-the-board-debug-usb)
4. [Daily use over Ethernet](#4-daily-use-over-ethernet)
5. [The capability view](#5-the-capability-view)
6. [When something goes wrong: the recovery ladder](#6-when-something-goes-wrong-the-recovery-ladder)
7. [Asking for help](#7-asking-for-help)

## 1. What you need

- An MPS3 board with its power supply.
- The Debug USB cable (MPS3 Technical Reference Manual, section 2.18) and an Ethernet
  cable.
- A PC running Linux, Windows or macOS, with Python 3.10 or newer (or uv) and git.
- To debug the DUT: OpenOCD on your PATH, with its remote_bitbang adapter (on by
  default in OpenOCD builds).
- Access to the Harness Manager repository (it is private: ask SoC Labs).
- The harness bundle from SoC Labs: a directory of files for the SD card. The signed
  update channel (`harness-manager update`) is not live in 0.1.0, so for now the bundle
  comes from SoC Labs directly.

## 2. Install Harness Manager

Install with the serial extra, which the Debug USB needs:

```bash
git clone git@github.com:SoC-Labs/HarnessManager.git
HarnessManager/scripts/install.sh --with-serial
```

On Windows: `powershell -ExecutionPolicy Bypass -File HarnessManager\scripts\install.ps1 -WithSerial`.

Check it: `harness-manager version` prints `0.1.0`. Then try the app with demo boards:
`harness-manager app --demo`. [docs/INSTALL.md](INSTALL.md) has the details.

**Linux only:** your user needs access to serial ports. On Ubuntu and Debian:
`sudo usermod -aG dialout $USER`, then log out and back in.

## 3. First install on the board (Debug USB)

Do these steps in order. Step 2, the backup, is not optional: it is your way back to
the SD card as it is today.

1. **Connect and look.** Connect the Debug USB cable and the Ethernet cable, and power
   the board on. The configuration SD appears as a drive named `V2M-MPS3`. Run:

   ```bash
   harness-manager probe
   ```

   It lists the board with its USB links: the board controller's serial port and the SD
   drive. Typical names:

   | | Serial port (the MCC) | SD drive |
   |---|---|---|
   | Linux | `/dev/ttyUSB0` | `/media/$USER/V2M-MPS3` |
   | macOS | `/dev/cu.usbserial-…` | `/Volumes/V2M-MPS3` |
   | Windows | `COM7` | `E:\` |

   The Debug USB adds four serial ports; `probe` says which one is the MCC.

   In the commands below, `-` means "this board, over USB only", and `--serial` and
   `--volume` name the port and the drive.

2. **Back up the SD.**

   ```bash
   harness-manager sd - --volume /media/$USER/V2M-MPS3 backup ~/mps3-backups
   ```

   It writes one zip and prints its sha256. Keep the zip somewhere safe, off the board.

3. **Install the harness bundle.**

   ```bash
   harness-manager sd - --volume /media/$USER/V2M-MPS3 install ~/harness-bundle \
       --backup ~/mps3-backups/<the zip from step 2>
   ```

   It asks before it writes. It refuses without a backup, and it never writes the board
   controller's own firmware (`.ebf` files). Writing over USB is slow and can take
   several minutes. A slow write is not a failed one. **Do not unplug the cable, power
   off, or start a second write while it runs.** If it is interrupted anyway, the SD
   keeps a marker, and the next install refuses until you put the backup back
   (`harness-manager sd - --volume DRIVE restore <zip>`). Then run the install again.

4. **Reboot the board, so it loads the new harness.**

   ```bash
   harness-manager mcc - --serial /dev/ttyUSB0 reboot
   ```

   It asks first, then waits until the board has gone down and come back. You can also
   power-cycle the board with its switch.

5. **Check it over Ethernet.** Give your PC's Ethernet port an address on the board's
   network, for example `192.168.10.1` with netmask `255.255.255.0`. Then:

   ```bash
   harness-manager info 192.168.10.101
   ```

   The board should show the harness version and `control idle`. That is the end of the
   first install. You can unplug the Debug USB cable.

## 4. Daily use over Ethernet

Everything here needs only the Ethernet cable. `TARGET` is `192.168.10.101`.

```bash
harness-manager app                               # the app; add the board by its address
harness-manager overlays 192.168.10.101           # the designs that load on this harness
harness-manager program 192.168.10.101 nanosoc    # program the DUT partition
harness-manager console 192.168.10.101 uart0      # the DUT's UART0 (Ctrl-] exits)
harness-manager debug up 192.168.10.101           # OpenOCD; gdb connects to the port it prints
harness-manager reset 192.168.10.101              # reset the DUT
harness-manager clock 192.168.10.101 --dut-mhz 50 # set the DUT clock
harness-manager restore 192.168.10.101            # back to the baseline design
```

`program` checks that the design was built for the harness on your board before it
writes anything, asks you, then confirms the board loaded it.

Consoles: open them in the app, or run the `screen` command the app shows
(`screen /tmp/harness-manager-$USER/<board>/uart0`, Linux and macOS;
`harness-manager pty 192.168.10.101 uart0` prints it too). The app and `screen` can show
the same console at the same time. The DUT's UART0 runs at the rate the loaded design
was built with (76800 for nanosoc), so `screen` needs no baud argument.

Debug: connect gdb with `target extended-remote 127.0.0.1:<gdb port>`. Arm DS uses the
same port through its "Generic GDB" connection. In 0.1.0 the debug service also needs
the MPS3 OpenOCD configs from your clone: set
`HARNESS_MANAGER_MPS3_OPENOCD_DIR=<clone>/vendor/openocd`, then
`harness-manager daemon stop` so the service restarts with it.

The command and the app share one board session through a background service, so you
can use both at once. `harness-manager daemon status` shows the service.

## 5. The capability view

Harness Manager never guesses what your board can do. It asks the board, and it lists
each capability as available or not. A missing capability always says what it needs.

`harness-manager info 192.168.10.101` over Ethernet alone shows, for example:

```text
control    idle
can        clock_dut, console_dut, debug_dut, deploy_partial, health, identify, reset_dut
cannot     console_controller: needs the Debug USB cable
cannot     reboot_board: needs the Debug USB cable, a networked power plug, or the J7 mod + 'mcc' firmware
cannot     storage_backup: needs the Debug USB cable (or a card reader)
cannot     telemetry_temp: needs the Debug USB cable, a JTAG cable on J17, or newer harness firmware
cannot     power_cycle: needs a networked power plug in boards.toml (Shelly, Tasmota or NETIO)
```

The app shows the same list under **Details**, and greys out a button whose capability
is missing, with the reason next to it.

What the reasons mean:
- **"needs the Debug USB cable"**: plug it in, then run the command with `--serial` and
  `--volume`, or reopen the board in the app.
- **"needs harness firmware with '…'"**: your harness is older than the feature. A newer
  harness bundle adds it.
- **"needs a networked power plug in boards.toml"**: Harness Manager can switch a
  Shelly, Tasmota or NETIO plug. Add the plug to
  `~/.config/harness-manager/boards.toml`:

  ```toml
  [boards."mps3@192.168.10.101:6900".power]
  kind = "shelly_gen2"
  url = "http://192.168.10.50"
  ```

## 6. When something goes wrong: the recovery ladder

Start at the top. Go down one step only when the step above did not help.

1. **Ask the board.** `harness-manager info TARGET` (or the app's header) shows the
   harness state and what to do:
   - `busy`: another program holds the board. Close it, or wait for its job to finish.
   - `offline`: nothing answers. Check the power, the Ethernet cable, and your PC's
     address (step 3.5).
   - `wedged`: the harness took the connection and never replied. Reboot the board
     (step 4 below).
   - `rescue`: the harness has no bootable image. Install the bundle again (step 6).
2. **Load the baseline design.** `harness-manager restore TARGET`. This fixes a DUT
   design that misbehaves while the harness itself answers.
3. **Reset the DUT.** `harness-manager reset TARGET`.
4. **Reboot the board from its SD.** `harness-manager mcc - --serial PORT reboot`, with
   the Debug USB cable plugged in. This reloads the FPGA from the SD card, as at power-on.
5. **Power-cycle.** Switch the board off, wait ten seconds, switch it on. With a
   networked power plug in `boards.toml`: `harness-manager power cycle TARGET`.
6. **Put the SD back.** Plug in the Debug USB cable, then
   `harness-manager sd - --volume DRIVE restore ~/mps3-backups/<zip>` and reboot (step 4).
   Restoring the backup from your first install returns the SD to how it was before
   Harness Manager touched it. Then install the harness again (section 3, steps 3 and 4).
7. **Use a card reader.** If the board does not show its SD drive at all, power it off,
   take out the configuration microSD, and copy your backup onto it with a card reader
   on your PC: unzip the backup to the card's root. Put it back and power on.

Harness Manager never writes the board controller's firmware (`.ebf` files) and refuses
the controller commands that erase or reformat (FORMAT, DEL, EEPROM and others), so none
of these steps changes the board controller itself.

## 7. Asking for help

Send SoC Labs:
- the output of `harness-manager --json info TARGET`;
- `harness-manager version`;
- the service log, `~/.config/harness-manager/daemon.log`
  (`%USERPROFILE%\.config\harness-manager\daemon.log` on Windows);
- what you ran, and what it printed.
