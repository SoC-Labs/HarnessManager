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

One program owns a board at a time (the board's ports take one client each).
Each verb takes the board's session lock for as long as it runs; `attach`
keeps it until you `detach`. A verb that finds the board held exits 4 and
names the holder."""

QUICK_START = """\
    harness-manager probe --host 192.168.10.101     is a board there?
    harness-manager info 192.168.10.101             identity, health, what it can and cannot do
    harness-manager overlays 192.168.10.101         which designs load on this shell
    harness-manager program 192.168.10.101 nanosoc  program a partition (asks first; --yes skips)
    harness-manager console 192.168.10.101 uart0    stream the DUT's UART0 (Ctrl-C stops)
    harness-manager debug up 192.168.10.101         start OpenOCD for the loaded design
    harness-manager reset 192.168.10.101 dut        reset the DUT
    harness-manager restore 192.168.10.101          back to the baseline design (greybox)

Add --json for scripts and the GUI, --tsv for shell pipelines."""

SYSTEM = """\
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
console TARGET NAME [--for SECONDS]
    Stream a console (uart0, uart1, swo, ...) to stdout until Ctrl-C or --for.
    --tsv prints one NAME<TAB>TEXT row per line. --json needs --for and prints
    one object with the text collected.
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
same port through its "Generic GDB" connection."""

RESET = """\
reset TARGET [WHAT]
    Reset one part of the board; WHAT defaults to dut. Only the targets the
    board offers are accepted (the fielded MPS3 shell offers only dut); any
    other exits 2 and lists them.
clock TARGET [--dut-mhz N | --preset NAME]
    With no option, list the clocks. --dut-mhz 50 or --preset 50mhz sets the
    DUT clock and prints what the board reports back."""

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
