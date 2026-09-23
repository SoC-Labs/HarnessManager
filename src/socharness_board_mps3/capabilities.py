"""What the MPS3 can do, and what each capability needs.

Feature names are the shell's ``version.features`` strings. Features the
fielded firmware does not report yet (``stats``, ``identify``, ``reboot``,
``log``, ``mcc``) come from harness handover lanes A0–A13. Capabilities that
need them light up automatically once a board reports them.
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
    CapabilitySpec(C.CONSOLE_SHELL, "Shell console",
                   (via(L.USB_SERIAL), via(L.HUB), via(L.ETHERNET, features=("shell_console",))),
                   needs_hint=NEEDS_USB + ", or harness firmware with 'shell_console'"),
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
    CapabilitySpec(C.RESET_SHELL, "Restart the shell", (via(L.ETHERNET, features=("reboot",)),
                                                         via(L.HUB, features=("reboot",)))),
    CapabilitySpec(C.DISCOVER_NETWORK, "Find boards on the network",
                   (via(L.ETHERNET, features=("identify",)),)),
)
