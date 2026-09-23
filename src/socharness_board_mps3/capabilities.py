"""What the MPS3 can do, and what each capability needs.

Feature names are the shell's ``version.features`` strings (net-protocol.md
"version": bits 0-4 since v0.8, bits 5-12 appended in v0.11). Capabilities that
need a feature light up automatically once a board reports it.

Two harness generations answer the same wire (plan §10: the same firmware
service modules run bare-metal and under ``mps3-harnessd``). They differ in
the links around them: a Linux harness adds an SSH link (dropbear, key-only),
so the shell console becomes an SSH login there. The bare-metal Ethernet shell
console (harness handover A12, TCP 6939) is moot and will never ship, so it has
no route here.
"""

from __future__ import annotations

from socharness.core import capabilities as C
from socharness.core.capabilities import CapabilitySpec, via
from socharness.core.model import LinkKind as L

NEEDS_USB = "needs the Debug USB cable"

SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(C.IDENTIFY, "Identify the harness", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.HEALTH, "Health counters", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.DEPLOY_PARTIAL, "Program a partition", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.CONSOLE_DUT, "DUT consoles (UART0/UART1/SWO)", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.DEBUG_DUT, "Debug the DUT CPU (OpenOCD)", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.RESET_DUT, "Reset the DUT", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.CLOCK_DUT, "Set the DUT clock", (via(L.ETHERNET), via(L.HUB))),
    # FPGA UART lane 2 over the Debug USB, the hub, or (Linux harness) an SSH login.
    CapabilitySpec(C.CONSOLE_SHELL, "Shell console",
                   (via(L.USB_SERIAL), via(L.HUB), via(L.SSH)),
                   needs_hint=NEEDS_USB + ", or an SSH link to a Linux harness (the "
                   "bare-metal Ethernet 'shell_console' (A12) will not ship)"),
    CapabilitySpec(C.CONSOLE_CONTROLLER, "Board controller (MCC) console",
                   (via(L.USB_SERIAL), via(L.HUB)), needs_hint=NEEDS_USB),
    CapabilitySpec(C.REBOOT_BOARD, "Reboot the board (reload from SD)",
                   (via(L.USB_SERIAL), via(L.USB_MSD), via(L.HUB), via(L.SMART_POWER),
                    via(L.ETHERNET, features=("mcc",))),
                   needs_hint=NEEDS_USB + ", a networked power plug, or the J7 mod + 'mcc' firmware"),
    CapabilitySpec(C.CLOCK_BOARD, "Board oscillators",
                   (via(L.USB_SERIAL), via(L.HUB), via(L.ETHERNET, features=("mcc",))),
                   needs_hint=NEEDS_USB + ", or the J7 mod + 'mcc' firmware"),
    CapabilitySpec(C.STORAGE_BACKUP, "Back up the configuration SD", (via(L.USB_MSD), via(L.HUB)),
                   needs_hint=NEEDS_USB + " (or a card reader)"),
    CapabilitySpec(C.STORAGE_INSTALL, "Install a harness onto the SD", (via(L.USB_MSD), via(L.HUB)),
                   needs_hint=NEEDS_USB + " (or a card reader)"),
    CapabilitySpec(C.TELEMETRY_TEMP, "Temperatures",
                   (via(L.USB_SERIAL), via(L.JTAG), via(L.HUB),
                    via(L.ETHERNET, features=("touch_temp",)), via(L.ETHERNET, features=("sysmon",))),
                   needs_hint=NEEDS_USB + ", a JTAG cable on J17, or newer harness firmware"),
    CapabilitySpec(C.TELEMETRY_POWER, "Board power", (via(L.SMART_POWER),),
                   needs_hint="the MPS3 has no power sensor; add a metered plug or an INA260"),
    # Only an outlet can cycle; a meter-only INA260 also gives SMART_POWER, so
    # Engine.info() narrows this by the adapter's cycle_reason.
    CapabilitySpec(C.POWER_CYCLE, "Power-cycle the board (cold)", (via(L.SMART_POWER),),
                   needs_hint="a networked power plug in boards.toml (Shelly, Tasmota, NETIO)"),
    # v0.11 `reboot`: a warm restart of the shell CPU (bare-metal: the watchdog,
    # ~3 s; Linux: sync + WDOG + a full OS boot, see constants.reboot_wait_s).
    CapabilitySpec(C.RESET_SHELL, "Restart the shell", (via(L.ETHERNET, features=("reboot",)),
                                                         via(L.HUB, features=("reboot",)))),
    CapabilitySpec(C.DISCOVER_NETWORK, "Find boards on the network",
                   (via(L.ETHERNET, features=("identify",)),)),
    # Pack-specific "lab" capabilities (the CLI's `lab` verbs).
    CapabilitySpec("mps3.display_flip", "Hand the CLCD panel to the DUT",
                   (via(L.ETHERNET, features=("clcd_kvm",)), via(L.HUB, features=("clcd_kvm",)))),
    # DUT egress (dutrx): v0.11 reports `dut_egress`, but the fielded 0x3F1A560F
    # image predates the bit while its fabric answers the verb, so this stays
    # gated on the link only (a fabric without the block answers
    # "dut_egress not present", which the lab verb reports as such).
    CapabilitySpec("mps3.dut_egress", "Read frames the DUT transmitted",
                   (via(L.ETHERNET), via(L.HUB))),
)

#: What each harness state means, and what to do about it. ``shell.py`` puts these
#: into ``Health.notes``; ``identify.py`` into a rescue candidate's evidence.
#: Keys are ``harness.<Health.control_channel or condition>``.
HARNESS_STATES: dict[str, str] = {
    "harness.idle": "the harness answers its control channel",
    "harness.busy": ("another client holds the control channel (it serves one client at a "
                     "time); close the other tool, or wait for its swap to finish"),
    "harness.wedged": ("the harness accepted the connection and never replied: its service "
                       "loop is hung. Linux: restart mps3-harnessd or send `reboot` over "
                       "SSH; bare-metal: MCC REBOOT"),
    "harness.offline": ("nothing accepts the control channel: check power, the Ethernet "
                        "cable and the board's IP"),
    "harness.service_down": ("the board is up, but its harness service is not listening on "
                             "the control channel (mps3-harnessd dead or respawning); wait "
                             "a few seconds, then restart it over SSH or reboot the board"),
    "harness.rescue": ("stage0 is in RESCUE: it answers ICMP, TFTP (69) and identify only, "
                       "with no control channel (no 6900). The microSD holds no bootable OS "
                       "slot (missing, blank or failed CRC); re-provision the card or push "
                       "an OS image over TFTP"),
}
