"""The tool owns its help text. The GUI renders it; nothing else keeps a copy.

``harness-manager help --tabs`` prints every section, each opened by a line
``## <tab name>`` and followed by plain text, in the order a help dialog shows
them. ``harness-manager help --tabs TAB`` prints one. The tables of TSV columns and
exit codes are GENERATED from ``output.TSV_COLUMNS`` and ``core.errors.ExitCode``
at print time, so the help cannot drift from the code.
"""

from __future__ import annotations

from collections.abc import Callable

from harness_manager.core.errors import ExitCode

from .output import TSV_COLUMNS

EXIT_MEANINGS: dict[ExitCode, str] = {
    ExitCode.OK: "done",
    ExitCode.FAILED: "an internal failure (a bug); the message says so",
    ExitCode.USAGE: "bad arguments or an unknown TARGET spelling",
    ExitCode.ABSENT: "the thing asked for does not exist (no board, overlay, console, file)",
    ExitCode.HELD: "someone else holds it (session lock, single-client port); the holder is named",
    ExitCode.PORT_BOUND: "a local port we need is already in use",
    ExitCode.ACTION_FAILED: "the board refused, or the action did not complete (or was inconclusive)",
    ExitCode.UNREACHABLE: "transport failure: no route, timeout, connection refused",
    ExitCode.ALREADY: "already in the requested state (already attached, not attached)",
    ExitCode.UNAVAILABLE: "the capability is unavailable here; the reason says what is missing",
    ExitCode.NOTHING_ON_TARGET: "the link is fine but nothing answered (no debug port in the design)",
    ExitCode.INCOMPATIBLE: "identity mismatch (the overlay was built for another shell)",
    ExitCode.REFUSED: "refused by a safety rule or not confirmed (no SD backup, denied MCC command)",
}

OVERVIEW = """\
harness-manager manages an FPGA prototyping harness: find a board, hold it, program
partitions, open consoles, debug the DUT, reset it, set clocks, and look after
the board controller and its configuration SD. The Arm MPS3 is the pilot board.

Every verb prints ONE result on stdout: human text by default, one JSON object
with --json, or tab-separated rows with --tsv. Progress, prompts and remarks go
to stderr. Errors are one line on stderr:

    harness-manager: <what went wrong> — <what to do next>

and the exit code says which kind of failure it was (see "Exit codes").

TARGET, in every verb that talks to a board:
    host[:port]      the shell's control address, e.g. 192.168.10.101 (port 6900)
    [v6addr]:port    an IPv6 literal, in brackets
    -                no Ethernet link: a USB-only board (give --serial and/or --volume)
    --serial URL     add the board controller's USB serial link:
                     serial:///dev/ttyUSB0, serial://COM7, or a bare /dev/ttyUSB0 or COM7
    --volume PATH    add the configuration SD volume (the mounted V2M-MPS3 drive)
    --via ssh:HOST   reach the shell through an SSH tunnel on HOST
    --via hub        reach it through the hub its boards.toml hub table names
                     (without --via, the board's boards.toml via does the same)

One program owns a board at a time (the board's ports take one client each).
Each verb takes the board's session lock for as long as it runs; `attach`
keeps it until you `detach`. A verb that finds the board held exits 4 and
names the holder.

`harness-manager help VERB` prints one verb's options. The app's Help shows these
sections; `harness-manager help --tabs` prints them."""

QUICK_START = """\
    harness-manager probe --host 192.168.10.101     is a board there?
    harness-manager info 192.168.10.101             identity, health, what it can and cannot do
    harness-manager overlays 192.168.10.101         which designs load on this shell
    harness-manager program 192.168.10.101 nanosoc  program a partition (asks first; --yes skips)
    harness-manager console 192.168.10.101 uart0    the DUT's UART0 here (Ctrl-] exits)
    harness-manager debug up 192.168.10.101         start OpenOCD for the loaded design
    harness-manager reset 192.168.10.101 dut        reset the DUT
    harness-manager restore 192.168.10.101          back to the baseline design (greybox)

Add --json for scripts and the GUI, --tsv for shell pipelines."""

SYSTEM = """\
version                    the Harness Manager version
packs                      the installed board packs
help [VERB] [--tabs [TAB]] one verb's options, or these help sections
probe [--host ADDR]... [--serial URL]... [--volume PATH]... [--timeout S] [--no-scan]
    Look for boards. Without addresses it scans the default shell address and
    USB. --no-scan looks only at the addresses given. Exit 3 if none answers.
info TARGET
    Identity (shell_id, rm_id, harness version, build check), health, and the
    capability view: every capability this board has, and for each one it
    lacks, the reason ("needs the Debug USB cable", "needs harness firmware with
    'stats'"). The GUI's System tab shows this.
attach TARGET [--note TEXT] [--for SECONDS]
    Take the board's session lock and hold it in the foreground until Ctrl-C,
    `detach`, or --for elapses (--for 0 takes and releases it at once: a check).
    Exit 4 names the holder when someone else has it; exit 8 if you already do.
    While you are attached, your own one-shot verbs are held off too: detach first.
detach TARGET [--timeout S]
    Release the hold of your own foreground verb (attach, console, debug up) by
    signalling that process, or clear a stale lock whose process is gone. It
    never releases anyone else's lock.
telemetry TARGET
    Every reading from every source, with where it came from. A reading that
    cannot be taken is shown as unavailable with its reason, never as 0."""

PROGRAM = """\
overlays TARGET [--overlay-dir DIR]...
    Every overlay the board knows: the ones that load on its shell, and the ones
    that do not, each with its reason.
program TARGET RM [--yes] [--keep-on-card] [--overlay-dir DIR]...
    RM is an overlay name (nanosoc) or rm_id (0x01000001). Prints the preflight
    table on stderr. A MISMATCH refuses and writes nothing: exit 14 when the
    overlay was built for another shell, exit 15 for any other failed check
    (corrupt payload, unpaired files). UNCHECKED items are not passes: they are
    shown, and the prompt decides. Asks before writing unless --yes. Progress
    goes to stderr; the result (rm_id, verified, seconds, transport) to stdout.
    A deploy the board did not confirm exits 6.
    --keep-on-card also keeps the design on the board's user microSD, so the
    board boots into it next time; without it the card is never written. It
    exits 12 before anything is written when the harness has no microSD store
    or no card is in the USER microSD slot. The result says whether it was kept
    (and in which slot) or why not; a card write that fails does not fail the
    program (the new design is running; the card keeps the one it had).
restore TARGET [--overlay-dir DIR]...
    Load the baseline design (greybox on the MPS3), then confirm it.

Where overlays come from: each overlay is a directory holding manifest.json and
its two payloads. --overlay-dir DIR (repeatable) is searched first, then the
directories in $HARNESS_MANAGER_MPS3_OVERLAY_DIRS (separated by the OS path
separator), then the local content store. DIR may be one overlay or a root of
several (<root>/<rm>/manifest.json, the platform's fpga/dfx/overlay/)."""

CONSOLES = """\
pty TARGET NAME [--baud RATE] [--for SECONDS]
    The console's PTY for `screen`: prints its path, then the command to attach
    with (`screen <path>`), for any terminal, while the web UI shows the same
    console. When harness-manager-daemon has the board open (the web UI), the
    PTY is the daemon's and this returns at once; otherwise this command holds
    it until Ctrl-C, `detach`, or --for. One terminal per PTY. Not on Windows
    (use --export). --baud sets a serial console's rate first.
baud TARGET NAME [RATE]
    The console's rate, where it comes from (serial, design, harness) and
    whether it can change. RATE changes it (0 = the default): a serial console
    reopens its port; uart0/uart1 need harness firmware with 'uart_baud' (the
    loaded design fixes 76800 otherwise). Exit 12 with the reason when it
    cannot change.
console TARGET NAME [--read-only] [--for SECONDS]
    A console (uart0, uart1, swo, ...) in this terminal, interactive: what you
    type goes to the board, Ctrl-C included (a MicroPython REPL needs it), and
    Ctrl-] exits. --read-only only shows the output. With --read-only, --for,
    --json or --tsv, or when stdout is not a terminal, it streams to stdout
    until Ctrl-C or --for. --tsv prints one NAME<TAB>TEXT row per line. --json
    needs --for and prints one object with the text collected.
console TARGET NAME --export PORT [--for SECONDS]
    Re-export the console on 127.0.0.1:PORT for an external terminal (PORT 0
    picks a free one). Prints the port on stdout, then holds until Ctrl-C or
    `detach`.
    Exit 5 if the port is taken. An unknown NAME exits 3 and lists the names."""

DEBUG = """\
debug up TARGET [--for SECONDS]
    Start OpenOCD for the loaded design, print the local gdb/telnet/tcl ports
    (127.0.0.1), and keep it up until Ctrl-C, `detach`, or --for elapses. This
    process owns the server and holds the board while it runs.
debug down TARGET          stop it
debug status TARGET        state, ports, config, pid
debug detect TARGET        non-intrusive: the TAP IDCODE, or exit 13 when the
                           loaded design has no debug port (greybox has none)

Connect gdb with `target extended-remote 127.0.0.1:<gdb port>`; Arm DS uses the
same port through its "Generic GDB" connection.

xvc open TARGET [--byo] [--for SECONDS]
    Debug the loaded design's ILAs in Vivado over XVC. It reaches the
    reconfigurable partition's debug chain only (the harness's Debug Bridge
    and the design's ILAs), never whole-device JTAG. Takes the board's one XVC
    slot and starts HM's hw_server, and holds them until Ctrl-C, `detach`, or
    --for; the session reopens after every partition swap. --byo runs only the
    relay, for your own hw_server. A hub board needs your lease.
xvc close TARGET           kick the client, stop hw_server, free the board's slot
xvc status TARGET          state, the Vivado URL, who is attached, the probes files
xvc tcl TARGET [--byo]     the Vivado Tcl snippet for the loaded design
xvc ltx TARGET [--rm | --static | --full] [-o FILE]
                           the probes file (.ltx) for the loaded design"""

RESET = """\
reset TARGET [WHAT]
    Reset one part of the board; WHAT defaults to dut. Only the targets the
    board offers are accepted (the fielded MPS3 shell offers only dut); any
    other exits 2 and lists them.
clock TARGET [--dut-mhz N | --preset NAME]
    With no option, list the clocks. --dut-mhz 50 or --preset 50mhz sets the
    DUT clock and prints what the board reports back.
power show TARGET
    W, V and A from the board's power meter (set up in boards.toml), and
    whether it can cycle the supply.
power cycle TARGET [--off S] [--yes]
    Cut the board's supply through the meter's outlet, then switch it back on
    (5 s off by default). Asks first unless --yes."""

LAB = """\
lab TARGET link up|down|pulse
    Inject a link event on the virtual PHY the DUT's Ethernet sees.
lab TARGET display harness|dut|toggle|query [--timeout S]
    Who drives the on-board CLCD panel. A flip waits for the handover to land;
    if it has not landed in time the verb says INCONCLUSIVE and exits 6.
lab TARGET macgen [--no-gen] [--no-chk] [--inject FAULT]
    Drive the MAC traffic generator/checker; prints its tx/rx/err counters.
lab TARGET dutrx [--frames N]
    Read up to N frames the DUT transmitted, with the capture counters. "No
    frame waiting" is a result (exit 0), not an error.
A shell whose bitstream lacks the block (no CLCD KVM, no DUT egress capture)
exits 12 and says so."""

CONTROLLER = """\
The board controller (the MCC on the MPS3) and the configuration SD are reached
over the Debug USB cable. Without it these verbs exit 12 with "needs the Debug
USB cable". For a board on USB only, TARGET is - :
    harness-manager mcc - --serial /dev/ttyUSB0 temp

mcc TARGET temp            controller temperatures
mcc TARGET osc             oscillator set-points
mcc TARGET reboot [--yes] [--wait S]
                           reboot and prove it (the board goes down, then comes
                           back); the running design is lost. Asks unless --yes.
mcc TARGET cmd LINE        one allowlisted controller command; destructive ones
                           (FORMAT, DEL, EEPROM, ...) are refused with exit 15.
sd TARGET backup DIR       back up the whole SD into a zip in DIR
sd TARGET install BUNDLE_DIR --backup ZIP [--yes]
                           write a harness bundle; the backup is mandatory, .ebf
                           files are never written. Runs after the next reboot.
sd TARGET restore ZIP [--yes]
                           put a backup back"""

PANEL = """\
panel show TARGET
    What the board's front panel (the LCD) shows: page, owner, banner, card,
    sessions, taps, Identify.
panel mirror TARGET
    The panel's text grid (read from the board, or rebuilt on bare metal).
identify TARGET [--seconds N]
    Blink the panel's backlight so you can tell which board it is (Linux
    harness): 1 to 30 seconds, 10 by default; 0 stops."""

HUBS = """\
A lab board sits behind a hub (fpgahub). No hub is the default: a board on your
desk needs none. Add the hub once, then the boards it offers.

hub add NAME --ssh HOST | --url URL [--token-stdin]
    Add a hub: your SSH account on HOST, or fpgahub's REST API with a token.
    --update changes an existing one.
hub list                   the hubs, and boards with an inline hub table
hub test NAME              test the connection (config, reach, auth, group,
                           targets, target); never takes a lease
hub targets NAME [--add TARGET]
                           what the hub offers; --add writes a board for one
hub token NAME --stdin | --ref REF | --clear
                           set, point at or forget your token for the hub
hub adopt BOARD [--as NAME]
                           turn a board's inline hub table into a named hub
hub remove NAME [--force]  remove a hub and your stored token for it

lease show TARGET          who holds it, until when; the queue and the requests
lease acquire TARGET [--ttl S]
                           take it; waits in the queue if someone holds it
lease release TARGET       give it back (only a lease this Harness Manager took)
lease request TARGET [--message M]
                           ask the holder to give it up; waits, with a 2:00
                           countdown
lease requests TARGET      the requests waiting for your answer
lease respond TARGET ID --release | --keep MINUTES
                           answer a request: release now, or keep it N minutes
lease force TARGET         force-release it after 2:00 with no answer (asks first)
lease leave TARGET         leave the queue and withdraw your request
lease dismiss TARGET       forget the last forced release of your lease
A lease needs a hub, and nothing takes one for you: run `lease acquire`, or use
Acquire lease in the app. The service renews it while the board is open there.
--via ssh:HOST (or via = "ssh:HOST" in boards.toml) is a tunnel only: no hub,
so no lease. --via hub goes through the board's hub.

share list TARGET          the hub's running TTY shares for the board
share start TARGET NAME    start one (mcc, fpga_uart2, ... from boards.toml, or a
                           /dev path). There is no stop: it stops every share."""

LINUX = """\
The Linux harness takes slot changes only over SSH, from the key that claimed it.

board claim TARGET [--key PUB] [--adopt] [--yes]
    Claim an unclaimed Linux harness with your SSH key and pin its host key
    (asks first; a hub board needs your lease). --adopt: the board is already
    claimed with your key.
board claim-status TARGET  is it claimed, and by this Harness Manager's key?
board ssh TARGET [-c CMD] [--print]
                           ssh in as root, with the pinned host key

slot status TARGET         OS slots A and B: running, default, where a push goes
slot push TARGET [IMAGE] --bundle PATH | --static-id ID [--yes]
    Push a boot image into the free slot and read it back; it is not
    committed. --bundle or --static-id names the static the image was
    provisioned for (never the board's own).
slot commit TARGET [--slot A|B]
                           the pushed slot boots next (at the next reboot)
slot rollback TARGET [--no-reboot] [--wait S]
                           the other slot becomes the default again, and boots
slot verify TARGET [--slot A|B]
                           read a slot back off the card

card status TARGET         the user microSD: present, the store, the power-on
                           default, the OS slots
card commit TARGET [--yes] the RUNNING overlay becomes the power-on default
card clear TARGET [--yes]  no power-on default: the greybox loads at power-on
No card: the board boots exactly as it always has. The card verbs need a harness
with the microSD store. Every change asks first (--yes skips it), and a hub board
needs your lease."""

BUILD = """\
kit info [TARGET] [--static-id ID]
    The static a DUT is built for, its build kit, the Vivado it needs, and
    the sources. TARGET reads the static from a board.
kit fetch [TARGET] [--static-id ID] [--out DIR]
    Put the static's kit (the locked DCP, CRC-checked) in the cache; --out
    also exports it into DIR.
kit guide [TARGET] [--design NAME|FILE] [--build-dir DIR]
    The build steps, each with its state, and what to do next.
kit script [TARGET] --design NAME|FILE [--out DIR]
    Write build_rm.tcl, the kit and the XDC kit into DIR. Without --out it
    shows the files and writes nothing.
kit build DIR [--stop-after STAGE] [--jobs N]
    The Vivado command for a build directory. Harness Manager does not run
    Vivado yet: run the command yourself.
kit check RECEIPT|BUILD_DIR|PARTIAL [TARGET]
    Check a build receipt and its pair, or a bare partial, board-free.
kit pack RECEIPT|BUILD_DIR [--import]
    The overlay from a passed receipt; --import adds it to Program.
kit list                   the cached kits
kit verify DIR [TARGET]    check a kit directory (and it against a board)
kit import DIR|ZIP         add a kit (or a fielded/<sid>/ directory) to the cache

xdc info                   the board pack's pin model: board, fielded shell,
                           sources, built-in designs
xdc rm-kit [--design NAME|FILE] [--out DIR] [--static-id ID]
                           the RM kit: OOC XDC, connectivity sheet, pblock
                           facts, wrapper skeleton
xdc board [--design NAME|FILE] [--out DIR]
                           the full-board export: pins, IO standards by bank,
                           clocks"""

UPDATES = """\
update check [TARGET]
    Read-only: what the signed channel offers, and with TARGET the plan for
    that board.
update harness TARGET [--version V] [--yes]
    Install the channel's harness on a board (asks first). A re-key also needs
    --consent with the exact phrase the plan prints ("REKEY 0x...").
update app [--stage-only | --apply] [--yes]
    Update this app: download, stage side by side, switch. --apply restarts a
    running service onto it, and it rolls back by itself if the new version
    does not come up.
update status              the app's versions, bad marks, last check and apply
update rollback TARGET [--backup ZIP]
                           restore a board's config SD backup, reboot, confirm
update rollback --app      switch back to the previous app version
The service checks for app updates in the background: 60 s after it starts,
then every 6 h by default (the admin policy's check_interval).

harness list [TARGET] [--all]
                           the harness releases, with a verdict for TARGET
harness show VERSION [TARGET]
                           one release: identity, parts, notes, what changes
harness fetch VERSION [--kit]
                           download and verify a release into the cache now
harness install TARGET [VERSION] [--yes]
                           install a release on a board (asks first)
harness pin TARGET VERSION pin a board to a release (none newer is offered)
harness unpin TARGET       remove the board's pin
harness history TARGET [--limit N]
                           the board's last installs, newest first
harness rollback TARGET [--to VERSION | --backup ZIP]
                           re-install the previous release, or restore a backup
harness mirror DIR [--all] write an offline mirror: channels and blobs"""

SETTINGS = """\
config list [SECTION] [--all]
    Every setting, its value and where it came from: lock (the admin's),
    env, user, machine, pack or default.
config get KEY             one setting: value, source, lock, env shadowing
config set KEY VALUE [KEY VALUE ...]
                           change settings, all or nothing
config unset KEY           remove your value: back to the admin's or the default
config set-secret KEY      store a secret, read from stdin (never the command line)
config unset-secret KEY    remove a secret from the store (clear-secret: the same)
config path                every file the settings use, and where a new secret goes
config test SECTION [NAME] prove a section's settings work, e.g. config test hubs lab
It goes through the service when that runs, so the app's open windows see the
change. Secrets go into the OS keyring, else a private file."""

APP = """\
app [--demo] [--no-native] [--port N]
    Harness Manager in its own window (starts harness-manager-daemon if
    needed). --demo: scripted boards, no hardware.
ui [--demo] [--no-browser] [--port N] [--listen ADDR]
    The web UI in a browser (starts harness-manager-daemon if needed). It
    prints the URL; --no-browser only prints it (to open it through `ssh -L`).
daemon start [--demo] [--foreground]
daemon stop [--demo] [--force]
daemon status [--demo]
    harness-manager-daemon, the local service that owns the boards, so the
    CLI, the app and long sessions share one board session."""

TROUBLESHOOTING = """\
exit 7, "refused" or "did not answer"
    The shell is not reachable: check power, the Ethernet cable, and the address.
exit 4, "is in use"
    Another program holds the board; the message names it. If it is your own
    `harness-manager attach`, run `harness-manager detach TARGET`.
exit 12, "needs the Debug USB cable"
    Plug the Debug USB into this computer, or use a verb that works over Ethernet.
exit 12, "this build has no ... service yet"
    This installation lacks that service; `harness-manager version --json` shows
    which engine is running.
exit 13 from debug
    The loaded design has no debug port. Program one that has (nanosoc)."""


def _output_tab() -> str:
    lines = [
        "--json   one JSON object on stdout. Success: {\"ok\": true, ...}. Failure:",
        "         {\"ok\": false, \"error\": {\"code\", \"name\", \"message\", \"hint\", ...}}",
        "         (\"holder\" when held, \"capability\"/\"reason\" when unavailable).",
        "--tsv    tab-separated rows, no header. An empty field is '-'; a field never",
        "         holds a tab or newline; lists are ';'-joined. Columns are APPEND-ONLY:",
        "         read them by position, and ignore extra columns a newer version adds.",
        "",
        "TSV columns per verb:",
    ]
    width = max(len(v) for v in TSV_COLUMNS)
    for verb, cols in TSV_COLUMNS.items():
        lines.append(f"  {verb:<{width}}  {' '.join(cols)}")
    lines += ["", "Both --json and --tsv may be given before or after the verb."]
    return "\n".join(lines)


def _exit_codes_tab() -> str:
    return "\n".join(f"  {int(c):>2}  {c.name:<18} {EXIT_MEANINGS.get(c, '')}"
                     for c in ExitCode)


TABS: tuple[tuple[str, Callable[[], str]], ...] = (
    ("Overview", lambda: OVERVIEW),
    ("Quick start", lambda: QUICK_START),
    ("System", lambda: SYSTEM),
    ("Program", lambda: PROGRAM),
    ("Consoles", lambda: CONSOLES),
    ("Debug", lambda: DEBUG),
    ("Reset", lambda: RESET),
    ("Lab", lambda: LAB),
    ("Board controller", lambda: CONTROLLER),
    ("Front panel", lambda: PANEL),
    ("Hubs and leases", lambda: HUBS),
    ("Linux harness", lambda: LINUX),
    ("Build a DUT", lambda: BUILD),
    ("Updates", lambda: UPDATES),
    ("Settings", lambda: SETTINGS),
    ("App and service", lambda: APP),
    ("Output", _output_tab),
    ("Exit codes", _exit_codes_tab),
    ("Troubleshooting", lambda: TROUBLESHOOTING),
)


def tab_names() -> list[str]:
    return [name for name, _ in TABS]


def tabs(only: str | None = None) -> list[tuple[str, str]]:
    """``[(name, text)]``; ``only`` picks one tab (case-insensitive), or ``[]`` if unknown."""
    out = [(name, render()) for name, render in TABS]
    if only is None:
        return out
    return [(n, t) for n, t in out if n.lower() == only.strip().lower()]


def render_tabs(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"## {name}\n{text}" for name, text in sections) + "\n"


def parse_tabs(text: str) -> dict[str, str]:
    """The inverse of ``render_tabs``: ``{tab name: text}`` (what a GUI does with it)."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(body).strip("\n") for name, body in sections.items()}
