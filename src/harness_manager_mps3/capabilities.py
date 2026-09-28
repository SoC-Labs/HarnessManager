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

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import CapabilitySpec, via
from harness_manager.core.model import LinkKind as L

NEEDS_USB = "needs the Debug USB cable"
#: The front panel's reasons (P2). Bare metal keeps its panel as it is (decision P3).
#: PANEL-TRUTH (david, 2026-09-28): a reason says what THIS IMAGE does not report, by
#: capability and feature name, never a harness type guessed from a missing feature: the
#: Linux harness rc2_v6 lacks 'panel', 'presence' and 'locate' too. The UI names the type
#: only from the harness's own ``version.impl`` (``PanelSupport.impl``).
NEEDS_PANEL = ("needs the Ethernet link and a harness image that reports its front panel "
               "(harness feature 'panel' or 'clcd_kvm')")
NEEDS_LOCATE = "Identify isn't available on this harness image yet (harness feature 'locate')"
NEEDS_PRESENCE = ("this harness image doesn't report who is connected "
                  "(harness feature 'presence')")
#: The live display (lane LM2). The adapter (``display.py``) says which of these is missing.
NEEDS_LCD_MIRROR = "needs the Linux harness with lcd_mirror and a claimed board"
#: FIX-PACK-2 item 1: "Find boards on the network" is UDP 6899 identify, which every Linux
#: harness since v0.11 and bare metal since FOLD A-v0.12 serves, and none lists as a
#: feature. So it needs only an Ethernet link; the session's ``capability_reasons`` makes a
#: recent identify answer the proof (identify.DiscoverWitness), and names a hub or tunnel.
NEEDS_ETHERNET_LAN = ("needs an Ethernet link on the board's own network (identify is UDP "
                      "6899)")
#: FIX-PACK-2 item 2: the MCC through the harness (net-protocol v0.18, mint 4): the features
#: ``mccif`` (in-fabric SCC) and ``mcc_local`` (the USB loopback), with the route read from
#: ``mcc status`` (``harness_mcc.py``); ``mcc`` is the pre-v0.18 alias (the J7 mod).
HARNESS_MCC = ("mccif", "mcc_local", "mcc")
NEEDS_MCC_HARNESS = "harness firmware with 'mccif' or 'mcc_local' (net-protocol v0.18)"


def _via_harness_mcc(*links: L) -> tuple:
    """One route per harness MCC feature name (any of them; ``mcc status`` says the route)."""
    return tuple(via(*links, features=(f,)) for f in HARNESS_MCC)


SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(C.IDENTIFY, "Identify the harness", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.HEALTH, "Health counters", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.DEPLOY_PARTIAL, "Program a partition", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.CONSOLE_DUT, "DUT consoles (UART0/UART1/SWO)", (via(L.ETHERNET), via(L.HUB))),
    CapabilitySpec(C.DEBUG_DUT, "Debug the DUT CPU (OpenOCD)", (via(L.ETHERNET), via(L.HUB))),
    # CCR X-7 (lane XVC-CORE): the harness's OWN XVC server, scoped to the reconfigurable
    # partition's debug chain (the Debug Bridge and the loaded design's ILAs); never
    # whole-device JTAG. The Linux harness is also reached by board SSH (D-X1).
    CapabilitySpec(C.DEBUG_FABRIC, "Debug the partition's ILAs (XVC, partition-scoped)",
                   (via(L.ETHERNET, features=("xvc_dbgbr",)), via(L.HUB, features=("xvc_dbgbr",)),
                    via(L.SSH, features=("xvc_dbgbr",)))),
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
                    *_via_harness_mcc(L.ETHERNET)),
                   needs_hint=NEEDS_USB + ", a networked power plug, or " + NEEDS_MCC_HARNESS),
    CapabilitySpec(C.CLOCK_BOARD, "Board oscillators",
                   (via(L.USB_SERIAL), via(L.HUB), *_via_harness_mcc(L.ETHERNET)),
                   needs_hint=NEEDS_USB + ", or " + NEEDS_MCC_HARNESS),
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
                   needs_hint="needs a networked power plug in boards.toml (Shelly, Tasmota or NETIO)"),
    # v0.11 `reboot`: a warm restart of the shell CPU (bare-metal: the watchdog,
    # ~3 s; Linux: sync + WDOG + a full OS boot, see constants.reboot_wait_s).
    CapabilitySpec(C.RESET_SHELL, "Restart the shell", (via(L.ETHERNET, features=("reboot",)),
                                                         via(L.HUB, features=("reboot",)))),
    CapabilitySpec(C.DISCOVER_NETWORK, "Find boards on the network", (via(L.ETHERNET),),
                   needs_hint=NEEDS_ETHERNET_LAN),
    # Pack-specific "lab" capabilities (the CLI's `lab` verbs).
    CapabilitySpec("mps3.display_flip", "Hand the CLCD panel to the DUT",
                   (via(L.ETHERNET, features=("clcd_kvm",)), via(L.HUB, features=("clcd_kvm",)))),
    # DUT egress (dutrx): v0.11 reports `dut_egress`, but the fielded 0x3F1A560F
    # image predates the bit while its fabric answers the verb, so this stays
    # gated on the link only (a fabric without the block answers
    # "dut_egress not present", which the lab verb reports as such).
    CapabilitySpec("mps3.dut_egress", "Read frames the DUT transmitted",
                   (via(L.ETHERNET), via(L.HUB))),
    # The front panel (lane P1/P2, docs/design/CLCD_ALIGNMENT.md §5.2). An image that
    # reports 'panel'/'presence'/'locate' has them; one with only 'clcd_kvm' (bare metal
    # v0.11, and Linux images before R1-R3) falls back to the KVM owner with a mirror
    # rebuilt from what Harness Manager read.
    CapabilitySpec(C.FRONT_PANEL, "Front panel (LCD) state",
                   (via(L.ETHERNET, features=("panel",)), via(L.HUB, features=("panel",)),
                    via(L.ETHERNET, features=("clcd_kvm",)), via(L.HUB, features=("clcd_kvm",))),
                   needs_hint=NEEDS_PANEL),
    CapabilitySpec(C.LOCATE, "Identify: blink this board's panel",
                   (via(L.ETHERNET, features=("locate",)), via(L.HUB, features=("locate",))),
                   needs_hint=NEEDS_LOCATE),
    CapabilitySpec(C.PRESENCE, "Show who is connected on the panel",
                   (via(L.ETHERNET, features=("presence",)), via(L.HUB, features=("presence",))),
                   needs_hint=NEEDS_PRESENCE),
    # The live, pixel-exact LCD mirror (lane LM2, docs/design/LCD_MIRROR.md §7.1). ``lcd_mirror``
    # is an ENGINE name in version.features (net-protocol v0.15: no bit), matched by name. The
    # board serves it on its loopback only, reached over the claimed board SSH: the Ethernet or
    # hub link is how HM finds the board, the SSH link how the demo names it. The adapter adds
    # what a link cannot say: claimed, and (D3) the lease holder only.
    CapabilitySpec(C.DISPLAY_MIRROR, "Live display",
                   (via(L.ETHERNET, features=("lcd_mirror",)), via(L.HUB, features=("lcd_mirror",)),
                    via(L.SSH, features=("lcd_mirror",))),
                   needs_hint=NEEDS_LCD_MIRROR),
)

#: What each harness state means, and what to do about it. ``shell.py`` puts these
#: into ``Health.notes``; ``identify.py`` into a rescue candidate's evidence.
#: Keys are ``harness.<Health.control_channel or condition>``.
HARNESS_STATES: dict[str, str] = {
    "harness.idle": "the harness answers its control channel",
    "harness.busy": ("another client holds the control channel (it serves one client at a "
                     "time); close the other tool, or wait for its swap to finish"),
    # A busy condition, not a state of its own: Health.control_channel is "busy".
    "harness.swap_settling": ("the harness is finishing a failed swap; it takes new clients "
                              "again within 30 s"),
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
