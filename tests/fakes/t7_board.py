"""T7 board-side fakes: an SD-bound identity, an OS A/B slot model, a fake ``uv``.

``bind_identity_to_sd(vb)``: VirtualMps3 (lead-owned) reloads "the boot design"
on every REBOOT but keeps the shell's identity fixed. A harness update needs the
board to come back as WHAT THE SD NOW HOLDS, so this wraps the FakeMcc's
``on_boot``: at "FPGA configuration complete." it follows the SD's
``board.txt -> APPFILE -> F0FILE`` chain (as the MCC does), reads the
``.bit`` it names, and if that is a T7 fake bitstream, the FakeShell restarts
with its static_id, harness version, sha and features. ``stale=True`` models a
board that keeps running the old image whatever the SD says (the
written-but-not-running case: a different APPFILE, an SD the MCC did not reread).

``FakeOsSlots``: the Linux harness's user-µSD slots A/B as the board's slot contract
has them (push -> staged -> commit -> reboot boots the default). ``bad_images`` holds
the image hashes that never come up healthy, so stage0 goes back to the old slot.

``FakeUv``: records every argv and imitates ``uv venv`` / ``uv pip install`` /
the venv's python, with switches to fail each step.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.services.update.os_slots import (
    SLOT_VALID,
    VERIFIED_BOOT,
    VERIFIED_NO,
    VERIFIED_READBACK,
    SlotInfo,
    SlotStatus,
)
from tests.fakes.t7_bundles import FIELDED_STATIC, read_fake_identity


def _sd_bit(root: Path) -> Path | None:
    """The .bit the MCC would load: board.txt APPFILE -> app note F0FILE (FAT, case-blind)."""
    board = root / "MB" / "HBI0309C" / "board.txt"
    try:
        app = re.search(r"APPFILE:\s*(\S+)", board.read_text()).group(1)   # type: ignore[union-attr]
        note = root / "MB" / "HBI0309C" / Path(*app.replace("\\", "/").split("/"))
        f0 = re.search(r"F0FILE:\s*(\S+)", note.read_text()).group(1)      # type: ignore[union-attr]
        return note.parent / f0
    except (OSError, AttributeError):
        return None


def bind_identity_to_sd(vb: Any, *, stale: bool = False) -> dict[str, Any]:
    """Make ``vb``'s shell report the identity of the bitstream on its SD after each boot.

    Returns a dict the test can inspect/modify: ``booted`` (identities seen at
    each boot) and ``stale`` (set True at any time to freeze the identity).
    """
    info: dict[str, Any] = {"booted": [], "stale": stale, "errors": []}
    original = vb.mcc.on_boot

    def on_boot() -> None:
        try:
            _boot()
        except Exception as exc:  # noqa: BLE001 - record it for the test, then fail as before
            import sys
            import traceback

            info["errors"].append(repr(exc))
            traceback.print_exc(file=sys.stderr)          # shown by pytest if the test fails
            raise

    def _boot() -> None:
        ident = None
        if not info["stale"]:
            bit = _sd_bit(vb.sd.root)
            if bit is not None and bit.is_file():
                ident = read_fake_identity(bit.read_bytes())
        if ident is not None:
            shell = vb.shell
            shell.static_id = int(ident["static_id"], 16)
            shell.config_agent.running_static_id = shell.static_id
            shell.harness_version = ident["harness"]
            shell.harness_sha = ident["sha"]
            shell.features = tuple(ident["features"])
            extra = getattr(shell, "version_extra", None)       # T12's HarnessFakeShell
            if isinstance(extra, dict):
                if ident.get("wire_usercode"):
                    extra["usercode"] = ident["wire_usercode"]
                else:
                    extra.pop("usercode", None)
        info["booted"].append(ident)
        original()

    vb.mcc.on_boot = on_boot
    return info


# --- the OS A/B slots -------------------------------------------------------------------


def _other(slot: str) -> str:
    return "B" if slot == "A" else "A"


@dataclass
class FakeOsSlots:
    """``OsSlotAdapter`` over an in-memory model of the board's slot contract (net-protocol
    v0.14 "Slot images"): a push goes to the slot that is neither running nor the default
    and is read back (``staged``); ``commit`` makes the staged slot the default; a reboot
    boots the default, unless its image is in ``bad_images`` (it never comes up healthy,
    so stage0 goes back to the old slot); the board confirms a healthy boot itself."""

    running: str = "A"
    default: str = "A"
    slots: dict[str, SlotInfo] = field(default_factory=lambda: {
        "A": SlotInfo("A", state=SLOT_VALID, hdr_crc="0xaaaaaaaa", length=64,
                      verified=VERIFIED_BOOT, image_sha256="a" * 64, version="1.0.0"),
        "B": SlotInfo("B")})
    staged: str = ""
    fabric_sid: str = FIELDED_STATIC
    bad_images: set[str] = field(default_factory=set)
    on_boot: Callable[[SlotInfo], None] | None = None     # the board comes up running this image
    reboot_fails: bool = False
    calls: list[str] = field(default_factory=list)

    @property
    def active(self) -> str:
        return self.running

    @active.setter
    def active(self, slot: str) -> None:
        self.running = self.default = slot

    def slots_reason(self) -> str:
        return ""

    def _target(self) -> str:
        return _other(self.running) if self.default == self.running else ""

    def status(self) -> SlotStatus:
        self.calls.append("status")
        return SlotStatus(running=self.running, slots=dict(self.slots), default=self.default,
                          target=self._target(), staged=self.staged, fabric_sid=self.fabric_sid)

    def push(self, image: Path, *, static_id: str, sha256: str = "", version: str = "",
             progress=None) -> SlotStatus:
        from harness_manager.core.errors import IncompatibleError, RefusedError

        target = self._target()
        if not target:
            raise RefusedError(f"no free slot: {self.running} runs, {self.default} is the "
                               "default -- rollback first")
        if int(static_id, 16) != int(self.fabric_sid, 16):
            raise IncompatibleError(f"image for {static_id} != fabric {self.fabric_sid}")
        self.calls.append(f"push:{target}")
        data = Path(image).read_bytes()
        import hashlib

        from tests.fakes.s0lb_image import header_crc

        actual = hashlib.sha256(data).hexdigest()
        self.slots[target] = SlotInfo(target, state=SLOT_VALID, hdr_crc=header_crc(data),
                                      length=len(data), sid=static_id,
                                      verified=VERIFIED_READBACK, image_sha256=actual,
                                      version=version)
        self.staged = target
        if progress:
            progress("readback", len(data), len(data))
        return self.status()

    def commit(self, slot: str | None = None) -> SlotStatus:
        from harness_manager.core.errors import RefusedError

        self.calls.append(f"commit:{slot or self.staged}")
        if not self.staged or (slot and slot != self.staged):
            raise RefusedError("nothing staged: push an image first")
        self.default = self.staged
        return self.status()

    def rollback(self, slot: str | None = None) -> SlotStatus:
        from harness_manager.core.errors import RefusedError

        dest = _other(self.default)
        self.calls.append(f"rollback:{dest}")
        if slot and slot != dest:
            raise RefusedError(f"slot mismatch: rollback would pick {dest}")
        if self.slots[dest].verified == VERIFIED_NO:
            raise RefusedError(f"slot {dest} not verified")
        self.default = dest
        return self.status()

    def verify(self, slot: str | None = None, progress=None) -> SlotStatus:
        dest = slot or _other(self.default)
        self.calls.append(f"verify:{dest}")
        info = self.slots[dest]
        if info.valid and info.verified == VERIFIED_NO:
            self.slots[dest] = replace(info, verified=VERIFIED_READBACK)
        return self.status()

    def reboot(self, progress=None, wait_s: float = 180.0) -> dict:
        self.calls.append("reboot")
        if self.reboot_fails:
            from harness_manager.core.errors import ActionFailedError

            raise ActionFailedError("reboot sent but no restart observed")
        want = self.default
        booted = want
        if self.slots[want].image_sha256 in self.bad_images:
            booted = self.running                 # never healthy: stage0 goes back
        self.running = booted
        self.staged = ""
        for name, info in list(self.slots.items()):  # what this boot knew is gone
            self.slots[name] = replace(info, verified=VERIFIED_BOOT if name == booted and
                                       info.valid else VERIFIED_NO)
        if self.on_boot:
            self.on_boot(self.slots[booted])
        if progress:
            for i, phase in enumerate(("sent", "down", "up"), 1):
                progress(phase, i, 3)
        return {"summary": f"booted slot {booted}", "slot": booted}


class StubSession:
    """A board session with a settable identity and optional adapters (unit tests)."""

    def __init__(self, identity: BoardIdentity, *, board_id: str = "mps3@stub",
                 storage: Any = None, controller: Any = None, os_slots: Any = None) -> None:
        self.ident = identity
        self.candidate = Candidate(pack="mps3", board_id=board_id,
                                   links=(Link(LinkKind.ETHERNET, "stub:6900"),))
        self.storage = storage
        self.controller = controller
        self.os_slots = os_slots
        self.identity_error: Exception | None = None

    def identity(self) -> BoardIdentity:
        if self.identity_error is not None:
            raise self.identity_error
        return self.ident


# --- the fake uv -----------------------------------------------------------------------


@dataclass
class FakeUv:
    """Imitates ``uv`` and a venv's python for AppUpdater. Nothing is installed or fetched."""

    fail_venv: bool = False
    fail_install: bool = False
    report_version: str | None = None      # what the new venv's python claims (None: the wheel's)
    calls: list[list[str]] = field(default_factory=list)
    installed: dict[str, str] = field(default_factory=dict)   # venv python path -> version
    reqs_seen: list[str] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if len(argv) > 1 and argv[1] == "venv":
            if self.fail_venv:
                return subprocess.CompletedProcess(argv, 2, "", "error: no Python 3.11 found")
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("#!fake python\n")
            (venv / "Scripts").mkdir(parents=True, exist_ok=True)
            (venv / "Scripts" / "python.exe").write_text("fake python\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 2 and argv[1:3] == ["pip", "install"]:
            if self.fail_install:
                return subprocess.CompletedProcess(argv, 1, "",
                                                   "error: hash mismatch for harness-manager")
            py = argv[argv.index("--python") + 1]
            reqs = Path(argv[argv.index("-r") + 1]).read_text()
            self.reqs_seen.append(reqs)
            m = re.search(r"harness_manager-([^-]+)-py3", reqs)
            self.installed[py] = m.group(1) if m else "?"
            return subprocess.CompletedProcess(argv, 0, "Installed 1 package", "")
        if len(argv) > 1 and argv[1] == "-c":
            ver = self.report_version or self.installed.get(argv[0], "")
            if not ver:
                return subprocess.CompletedProcess(argv, 1, "", "ModuleNotFoundError: harness-manager")
            return subprocess.CompletedProcess(argv, 0, ver, "")
        return subprocess.CompletedProcess(argv, 127, "", f"fake uv: unknown command {argv}")
