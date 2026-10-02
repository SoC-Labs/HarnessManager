# Windows: every user-visible text (lane WINDOWS, HM v1.1.0)

For the guide lead's self-install chapter (due Wed 14 Oct). The network and card-writer
blocks below are GENERATED from the code on fixtures (the lane's
`tests/fakes/win_net.py`, `win_disks.py`), so they are what Harness Manager prints today;
the rest is quoted from the code. Placeholders: `<adapter>` is the user's adapter name,
`N` the card's disk number, `<sha256>` the image's. Sources in brackets.

None of this has run on a real Windows laptop yet: see "Proven only on Windows" at the end.

## Installer (scripts/install.ps1)

The command:

```powershell
powershell -ExecutionPolicy Bypass -File HarnessManager\scripts\install.ps1 -WithSerial
powershell -ExecutionPolicy Bypass -File D:\wh-win\install.ps1 -Offline D:\wh-win -WithSerial
```

Lines it prints (new in v1.1.0):

```text
pins     the tested dependency versions (constraints.txt; -Latest for the newest)
offline  only from D:\wh-win (no package index)
latest   rebuilding <venv> with the newest dependency versions
menu     C:\Users\ann\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Harness Manager.lnk
removed  C:\Users\ann\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Harness Manager.lnk
install.ps1: could not add Harness Manager to the Start menu (<why>); run 'harness-manager app' instead
install.ps1: error: -Offline D:\x is not a directory (make one on a machine with network: scripts/make_wheelhouse.sh --platform win_amd64 --python-version 3.12 DIR)
install.ps1: error: -Offline cannot clone <url>; give a checkout or a wheel
install.ps1: error: the install did not finish (pip exited 1; the tool's message is above).
  -Offline: D:\wh-win lacks a wheel this needs. Make the wheelhouse for this
  laptop's Python: scripts/make_wheelhouse.sh --platform win_amd64 --python-version X.Y DIR
  (X.Y: what 'py --version' or 'python --version' prints here).
  Running this again is safe: it resumes.
install.ps1: error: the install did not finish (pip exited 1; the tool's message is above).
  If it could not reach the package index (PyPI):
  - check the network; behind a proxy: $env:HTTPS_PROXY = 'http://PROXY:PORT'
  - with no network: make a wheelhouse elsewhere, then -Offline DIR (docs\INSTALL.md)
  If a pinned version has no wheel for this Python, try -Latest.
  Running this again is safe: it resumes.
Next:
  harness-manager app             the app (also in the Start menu: Harness Manager)
```

Start menu entry: **Harness Manager**, "SoC Labs Harness Manager: bring up and use MPS3
boards" (runs `harness-manager app`, console minimised).

Wheelhouse maker (Linux, `scripts/make_wheelhouse.sh --platform win_amd64 --python-version 3.12 DIR`):

```text
wheelhouse /path/DIR (N wheels, win_amd64, Python 3.12)
install with: powershell -ExecutionPolicy Bypass -File DIR\install.ps1 -Offline DIR
make_wheelhouse: --platform win_amd64 needs --python-version (the laptop's Python, as 3.12)
wheelhouse_cross: pip could not download a win_amd64 wheel for Python 3.11 of: <specs> (exit 1). A package with no wheel for win_amd64 cannot go in a wheelhouse; try another --python-version
```

## The wizard on Windows (js/bringup.js)

- Over USB, nothing found: "No MPS3 Debug USB found on this PC." then the four Windows checks
  (below, "Debug USB").
- Scan, nothing on Ethernet: the board row's Ethernet line "nothing answers" and, below it,
  the network block (below, "Network check"), headed **This Windows PC's network** (the
  board at 192.168.10.101): one numbered item per problem: **<title>.** <text>, then "In
  PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as
  administrator, Yes; paste each line):" and each command with a **Copy** button; "Or
  instead:" and the alternative; the Settings route; closing "Harness Manager never runs these
  itself. Then scan or wait again."
- Step 4, **Wait for the harness**, timed out: "Nothing answered at 192.168.10.101 within
  180 s. Check the cable and this PC's address, wait again, or restore the backup." then the
  same network block.
- Step 5, the Linux OS image, card reader chosen: "On Windows, Write gives you the steps
  instead of writing: Administrator PowerShell, or Raspberry Pi Imager. Harness Manager never
  asks for Administrator."
- Step 5 after **Write**: "Harness Manager never writes a whole card on Windows (it needs
  Administrator, and Harness Manager never asks for it). Nothing was written." and "Open
  PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as
  administrator, Yes), then paste each step in turn: the steps are below." Then the block:
  **Write it yourself**: open PowerShell as Administrator (...), then paste each step in turn: the five numbered steps, each
  with **Copy**; "Then check it (in the same Administrator PowerShell): it prints <sha256>."
  with the check and **Copy the check**; **Or with Raspberry Pi Imager**
  (https://www.raspberrypi.com/software/): the five imager lines.
- Identity proposal: "a free address of 192.168.10.110-199, searched from the MAC"; the
  note "every board gets its own IP: a free address of the pool, searched from its MAC (the
  image default 192.168.10.101 is never given)". Name this board, IP **Auto**: "A free address
  of the pool, searched from the MAC" (title) and "A free address of 192.168.10.110-199,
  searched from the MAC (its last byte picks the first one tried): not one given to another
  board here, and not one that answers now."

## `info`: the identify witness on Windows

`cannot discover: the board did not answer identify from here (<why>); a harness that serves
it (...) answers on the board's own network unless something drops UDP 6899. On this Windows
PC: <title>: <text> As Administrator: <command> ; <command>`

## Network check, CLI (probe/info on failure)

### no address (one 169.254 adapter)

```text
this PC's network (192.168.10.101, Windows):
  No address on the board's network: This PC has no address on 192.168.10.0/24, the board's network (192.168.10.101). Give the Ethernet adapter cabled to the board a fixed address there, e.g. 192.168.10.1 (netmask 255.255.255.0, no gateway). The adapter is 'Ethernet 2' (Realtek USB GbE Family Controller): it is up with only a 169.254.x.x address (no DHCP on a direct cable).
    in PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes; paste each line):
      Set-NetIPInterface -InterfaceAlias "Ethernet 2" -Dhcp Disabled
      New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress 192.168.10.1 -PrefixLength 24
    Or in Settings: Network & internet, Ethernet (the board's adapter), IP assignment, Edit, Manual, IPv4 on: IP address 192.168.10.1, Subnet mask 255.255.255.0, Gateway empty, Save
  Harness Manager never runs these itself.
```

### no address (two candidate adapters)

```text
this PC's network (192.168.10.101, Windows):
  No address on the board's network: This PC has no address on 192.168.10.0/24, the board's network (192.168.10.101). Give the Ethernet adapter cabled to the board a fixed address there, e.g. 192.168.10.1 (netmask 255.255.255.0, no gateway). Which adapter is cabled to the board? Up now: 'Ethernet 2' (Realtek USB GbE Family Controller); 'Ethernet 3' (Realtek USB GbE Family Controller). Get-NetAdapter lists them all.
    in PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes; paste each line):
      Set-NetIPInterface -InterfaceAlias "<the Ethernet adapter cabled to the board>" -Dhcp Disabled
      New-NetIPAddress -InterfaceAlias "<the Ethernet adapter cabled to the board>" -IPAddress 192.168.10.1 -PrefixLength 24
    Or in Settings: Network & internet, Ethernet (the board's adapter), IP assignment, Edit, Manual, IPv4 on: IP address 192.168.10.1, Subnet mask 255.255.255.0, Gateway empty, Save
  Harness Manager never runs these itself.
```

### Public profile

```text
this PC's network (192.168.10.101, Windows):
  The board's network is Public: Windows treats 'Ethernet 2' (Realtek USB GbE Family Controller), the board's network, as a Public network, and Windows Firewall drops the board's UDP replies there: finding boards, a board in stage0 RESCUE, a board that moved. Make the network Private (one line), or allow UDP from the board's network.
    in PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes; paste each line):
      Set-NetConnectionProfile -InterfaceAlias "Ethernet 2" -NetworkCategory Private
    or instead:
      New-NetFirewallRule -DisplayName "Harness Manager board UDP" -Direction Inbound -Protocol UDP -RemoteAddress 192.168.10.0/24 -Action Allow
  Harness Manager never runs these itself.
```

### identify silent, TCP works, Private

```text
this PC's network (192.168.10.101, Windows):
  The board's UDP replies are dropped: The board answers TCP at 192.168.10.101 but not UDP identify: a firewall on this PC drops its UDP replies ('Ethernet 2' (Realtek USB GbE Family Controller) is Private). Allow UDP from the board's network.
    in PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes; paste each line):
      New-NetFirewallRule -DisplayName "Harness Manager board UDP" -Direction Inbound -Protocol UDP -RemoteAddress 192.168.10.0/24 -Action Allow
  Harness Manager never runs these itself.
```

### Python block rule

```text
this PC's network (192.168.10.101, Windows):
  Windows Firewall blocks this Python: An inbound Block rule for Python (python.exe: made when a Windows Security Alert for Python was cancelled) wins over any allow rule, so the board's UDP replies never reach Harness Manager. Turn it off:
    in PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes; paste each line):
      Disable-NetFirewallRule -Name 'TCP Query User{...}C:\py\python.exe'
  Harness Manager never runs these itself.
```

### PowerShell could not read the adapters

```text
this PC's network (192.168.10.101, Windows):
  This PC's network could not be read: PowerShell could not read the network adapters: Get-NetAdapter : Access is denied. Check by hand: the board's adapter needs an address on the board's /24, and a Private network profile (Get-NetConnectionProfile)
  Harness Manager never runs these itself.
```

## Card writer: whole-card image on Windows (CLI)

```text
Harness Manager never writes a whole disk on Windows (\\.\PhysicalDrive2): open PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes), then paste each step in turn:
  1. $d = Get-Disk -Number 2; if ($d.Size -ne 15931539456 -or $d.IsSystem -or $d.IsBoot) { throw 'Disk 2 is not the card Harness Manager listed (Generic- MicroSD/M2 USB Device 15.9 GB): stop' }; $d | Format-Table Number, FriendlyName, BusType, Size
  2. Set-Content -Path "$env:TEMP\hm-clean-disk2.txt" -Value 'select disk 2', 'clean'; diskpart /s "$env:TEMP\hm-clean-disk2.txt"
  3. Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;public static class HmRawDisk{[DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)]public static extern SafeFileHandle CreateFile(string n,uint a,uint s,IntPtr p,uint c,uint f,IntPtr t);}'
  4. $h = [HmRawDisk]::CreateFile('\\.\PhysicalDrive2', 3221225472, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero); if ($h.IsInvalid) { throw 'cannot open disk 2: is this PowerShell running as Administrator?' }; $dst = New-Object IO.FileStream($h, [IO.FileAccess]::ReadWrite); $src = [IO.File]::OpenRead('C:\Users\ann\Downloads\mps3-card.img'); $buf = New-Object byte[] 4194304; [void]$src.Seek(512, 'Begin'); [void]$dst.Seek(512, 'Begin'); while (($k = $src.Read($buf, 0, $buf.Length)) -gt 0) { if ($k % 512) { [Array]::Clear($buf, $k, 512 - $k % 512); $k += 512 - $k % 512 }; $dst.Write($buf, 0, $k) }; [void]$src.Seek(0, 'Begin'); [void]$src.Read($buf, 0, 512); [void]$dst.Seek(0, 'Begin'); $dst.Write($buf, 0, 512); $dst.Flush(); $dst.Close(); $src.Close(); 'written'
  5. Update-Disk -Number 2
then check it (in the same Administrator PowerShell; it prints 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef):
    Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;public static class HmRawDisk{[DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)]public static extern SafeFileHandle CreateFile(string n,uint a,uint s,IntPtr p,uint c,uint f,IntPtr t);}'; $h = [HmRawDisk]::CreateFile('\\.\PhysicalDrive2', 2147483648, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero); $f = New-Object IO.FileStream($h, [IO.FileAccess]::Read); $sha = [Security.Cryptography.SHA256]::Create(); $buf = New-Object byte[] 4194304; $left = [long]536870912; while ($left -gt 0) { $want = [int][Math]::Min([long]$buf.Length, $left); $ask = [int]([Math]::Ceiling($want / 512) * 512); $got = $f.Read($buf, 0, $ask); if ($got -le 0) { break }; $use = [int][Math]::Min($got, $want); [void]$sha.TransformBlock($buf, 0, $use, $null, 0); $left -= $use }; [void]$sha.TransformFinalBlock($buf, 0, 0); $f.Close(); -join ($sha.Hash | ForEach-Object { $_.ToString('x2') })
Or with Raspberry Pi Imager (https://www.raspberrypi.com/software/):
  - check the image first (any PowerShell): Get-FileHash -Algorithm SHA256 'C:\Users\ann\Downloads\mps3-card.img' shows Hash 0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF
  - open Raspberry Pi Imager (it asks for Administrator itself: Yes)
  - Choose OS: Use custom, then C:\Users\ann\Downloads\mps3-card.img
  - Choose Storage: Generic- MicroSD/M2 USB Device 15.9 GB (nothing else)
  - Next; No to OS customisation; Yes to erase the card. It writes, then verifies what it wrote
```

## Card writer: other Windows texts

- files, no drive letter: `its FAT volume has no drive letter: give it one in Disk Management (Start, type diskmgmt.msc; right-click the volume, Change Drive Letter and Paths, Add)`
- excluded system disk: `holds Windows (this PC's system or boot disk)`
- excluded USB hard disk: `not removable (a fixed disk)`
- RealAccess refusal: `Harness Manager never writes a raw disk on Windows (\\.\PhysicalDriveN)` / hint `run the Administrator steps the write printed`
- unsupported OS: `writing SD cards in this PC's card reader is not supported on this operating system (Linux, macOS and Windows only): write the configuration SD over the board's Debug USB instead`
- wizard (OS step, Windows reader): `On Windows, Write gives you the steps instead of writing: Administrator PowerShell, or Raspberry Pi Imager. Harness Manager never asks for Administrator.`
- wizard (needs_privilege on Windows): `Harness Manager never writes a whole card on Windows (it needs Administrator, and Harness Manager never asks for it). Nothing was written.` then `Open PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes), then paste each step in turn: the steps are below.`
- wizard steps block: `Write it yourself: open PowerShell as Administrator (Start, type PowerShell, right-click Windows PowerShell, Run as administrator, Yes), then paste each step in turn:` / `Then check it (in the same Administrator PowerShell): it prints <sha256>.` / `Or with Raspberry Pi Imager (https://www.raspberrypi.com/software/):`

## Debug USB (probe, wizard scan)

- no COM ports (Windows): `no FT4232H serial ports (COM) were found: in Device Manager, Ports (COM & LPT) should list four 'USB Serial Port (COMn)' for the board. If they are missing, or under Other devices, install the FTDI VCP driver (Windows Update offers it; else ftdichip.com, VCP Drivers), then unplug and replug the Debug USB`
- wizard, no MCC port (Windows): `no MCC serial port with it: the board cannot be rebooted from here (power it off and on by hand after the write). In Device Manager, Ports (COM & LPT) should list four 'USB Serial Port (COMn)' for the board; if not, install the FTDI VCP driver (Windows Update, or ftdichip.com), then replug the Debug USB and Scan again`
- wizard, none found (Windows):
  - `the Debug USB cable: the board's DEBUG USB socket to this PC`
  - `the board's power: switch it on and wait about 10 s for the MCC to start`
  - `the V2M-MPS3 drive: Windows gives it a drive letter itself (File Explorer, This PC); if it is not there, try another USB port or cable`
  - `the serial ports: Device Manager, Ports (COM & LPT) lists four 'USB Serial Port (COMn)' for the board; if they are missing or under Other devices, install the FTDI VCP driver (Windows Update, or ftdichip.com), then replug the Debug USB`
- pyserial missing: `serial ports not scanned (pyserial is not installed, so serial:// ports cannot be opened; install pyserial: re-run the installer with the serial extra (Windows: install.ps1 -WithSerial; Linux and macOS: install.sh --with-serial), or pip install 'harness-manager[serial]')`

## SSH

- ssh missing (Windows): `install the OpenSSH client: Settings, System, Optional features, View features, OpenSSH Client (or, as Administrator: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0)`

## Seat sheet (david, 2 Oct)

```powershell
harness-manager board identity 192.168.10.101 --label WS-0N --ip 192.168.10.(110+N) --mac random
```

`board identity --help`, `--ip`: "the board's address in a /24, e.g. 192.168.10.117; auto: a
free address of the pool, searched from the board's MAC (MPS3: mps3.identity.ip_pool, first
try 192.168.10.(110 + mac[5] mod 90))".

## Proven only on Windows (the Tue 6 / Fri 9 checklist)

Kept in the lane's hand-back; each item names what to look at.

### Tue 6 Oct: remote, via the hub (no Debug USB needed)

1. `install.ps1 -WithSerial` in Windows PowerShell 5.1 AND PowerShell 7: the Start menu entry
   appears and opens the app (console minimised); `-NoStartMenu` removes it; `-Uninstall`
   removes it. A user name WITH A SPACE (make a "Test User" account).
2. Offline: on Linux `make_wheelhouse.sh --platform win_amd64 --python-version <the
   laptop's> --with-serial DIR`; copy DIR; network OFF; `DIR\install.ps1 -Offline DIR
   -WithSerial`; `harness-manager version`. Note any wheel pip asks for that is not there.
3. `harness-manager --json flash devices` (bringup.sd_flash on) with a USB card reader and
   a card: record the JSON (BusType/PartitionStyle as names or numbers? DriveLetter? the
   reader's MediaType "Removable Media"?); the system disk and any USB hard disk excluded.
4. The network check: a USB Ethernet adapter, unconfigured (169.254): `harness-manager probe
   --host 192.168.10.101` names THAT adapter (no_address); paste its two lines; then the
   profile shows Public (public_profile); paste `Set-NetConnectionProfile ... Private`; the
   check goes quiet. Record `Get-NetConnectionProfile | ConvertTo-Json` (Category as a name?).
5. The first identify from Python: does Windows show "Windows Defender Firewall has blocked
   some features of this app"? Press Cancel, then `probe`: is the Block rule found
   (python_blocked) under sys.executable or the venv's base python.exe? Does the printed
   `Disable-NetFirewallRule -Name '...'` work as pasted?
6. No console window flashes from the app: the wizard's scan (PowerShell), `flash devices`,
   a claim through the hub (ssh.exe), a tunnel. Task Manager after closing the app: no
   ssh.exe left behind.
7. SSH through the hub: claim a Linux board, `board ssh`, the debug forward. Check ssh.exe
   accepts `GlobalKnownHostsFile=/dev/null`, a `UserKnownHostsFile="C:/Users/Test
   User/..."` (quoted, forward slashes), and `-J` (ProxyJump) by ssh.exe's full path.
8. ping: `probe --host` an address on the board's /24 with nothing there: no "answered ping"
   from a "Destination host unreachable" reply.

### Fri 9 Oct: physical (a board, its Debug USB, a card reader)

1. Plug the Debug USB: Windows Update installs the FTDI driver? Device Manager shows four
   COM ports. `harness-manager --json probe --no-scan` (USB): record pyserial's
   `serial_number` per port (the channel letter A-D?) and `location`. The MCC is the A port.
   If the chip has no serial number the grouping falls back to "interface numbers unknown":
   record exactly what pyserial reports.
2. The V2M-MPS3 drive letter: `sd - --volume E: backup DIR` (a bare `E:`), install over USB,
   `mcc - --serial COMn reboot`.
3. The wizard end to end over USB; the witness with the fixed address; a Public profile
   (identify silent: RESCUE not seen) then Private (seen).
4. The card reader, `files`: the config card out of the board, in the reader; backup and
   write; back in the board; it boots.
5. The card reader, `card`: paste the five Administrator steps VERBATIM on a real microSD
   (diskpart clean on removable media; CreateFile; MBR last; Update-Disk); the read-back
   prints the image's sha256; the board boots slot A. Then the same with Raspberry Pi Imager
   "Use custom". Watch for "You need to format the disk" pop-ups mid-write.
6. Consoles: `harness-manager console` on the COM port (interface 02) in Windows Terminal;
   Ctrl-] q; the app's console.
7. A Linux board claimed directly (no hub): the key at %USERPROFILE%\.ssh\id_ed25519 (OpenSSH
   for Windows refuses a private key other users can read), on-board OpenOCD, and
   arm-none-eabi-gdb.exe attaching to the 127.0.0.1 port shown.
8. The seat sheet: `board identity 192.168.10.101 --label WS-03 --ip 192.168.10.113 --mac
   random`; the laptop at 192.168.10.1 reaches it after the restart. `--ip auto`'s first try
   is 192.168.10.(110 + mac[5] mod 90).
9. Two `flash write` at once on the same card: the second is refused (msvcrt lock).
