"""The July Linux harness (net-protocol v0.7), for the B0 slot 3 rehearsal (lane L1).

What B0 slot 3 meets (platform docs/planning/B0_RUNBOOK_LINUX.md, "Slot 3"): the
board still running the July Linux image from slot 2 (static ``0x2B082E1B``,
``mps3-ctrld``/``mps3-pushd``, the v0.7 daemons). Modelled from the runbook and
its QEMU proof log (``qemu_1region_ssh_proof.log``, 2026-09-23):

- ``ping`` answers ``{"ok":true,"shell_id":"0x2b082e1b","rm_id":"0x00000000"}``
  (``rm_id`` is ``0x0100001e`` after the S2.9 ``led`` swap);
- there is NO ``version`` verb: ``{"op":"version"}`` answers exactly
  ``{"ok":false,"err":"unknown op"}``, with no op name in the text (the
  installed FakeShell says ``unknown op 'version'``, so this profile overrides it);
  the v0.11 verbs (``stats``, ``log``, ``reboot``) are unknown the same way;
- ``diag`` answers, without the counters Linux has no source for (the same
  omissions as ``t12_harness_shell.LINUX_OMITTED_DIAG_KEYS``: an assumption, the
  runbook only says diag "still works");
- the 6910 push path works (S2.9 swapped ``led`` over it);
- no UDP identify responder (v0.7 predates it).

``JulyLinuxBoard`` is a ``VirtualMps3`` whose shell is ``JulyV07Shell``, so the
rest of the rig (the MCC, the SD, the tunnel routes) is unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .t12_harness_shell import LINUX_OMITTED_DIAG_KEYS, HarnessFakeShell
from .virtual_board import FIELDED_3F1A560F, FirmwareProfile, VirtualMps3

JULY_LINUX_STATIC_ID = 0x2B082E1B
LED_RM_ID = 0x0100001E

#: The July image: no ``version``, so no features, no harness version, no impl.
JULY_LINUX_V07 = replace(
    FIELDED_3F1A560F, name="july-linux-v0.7-0x2B082E1B", static_id=JULY_LINUX_STATIC_ID,
    harness_version="", harness_sha="", features=(), usr_access=None, impl=None,
    omit_diag_keys=LINUX_OMITTED_DIAG_KEYS)

UNKNOWN_OP = {"ok": False, "err": "unknown op"}


class JulyV07Shell(HarnessFakeShell):
    """``mps3-ctrld`` v0.7: the verbs it has, and the exact reply for the ones it does not."""

    KNOWN = ("ping", "reset", "set_clk", "swap", "commit", "diag", "link")

    def handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") not in self.KNOWN:
            return dict(UNKNOWN_OP)
        return super().handle_control(request)

    def _op_version(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(UNKNOWN_OP)


class JulyLinuxBoard(VirtualMps3):
    def __init__(self, tmp_path: Path, *, boot_rm_id: int = 0, usb: bool = False) -> None:
        super().__init__(tmp_path, JULY_LINUX_V07, boot_rm_id=boot_rm_id, usb=usb)
        p: FirmwareProfile = JULY_LINUX_V07
        self.shell = JulyV07Shell(
            "127.0.0.1", features=(), omit_diag_keys=p.omit_diag_keys,
            static_id=p.static_id, boot_rm_id=boot_rm_id, reset_targets=p.reset_targets,
            harness_version=p.harness_version or "0.0.0", harness_sha=p.harness_sha,
            harness_usr_access=p.usr_access, harness_ver32=0,
            control_port=0, tftp_port=0, raw_tcp_port=0, uart0_port=0, uart1_port=0, swo_port=0)
