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

``FakeOsSlots``: the Linux harness's user-µSD slots A/B with stage0's
try-once/confirm rule: a try-once slot that is not confirmed healthy before the
next boot is abandoned and the old slot boots again. ``bad_images`` holds the
image hashes that "panic", so stage0 rolls back.

``FakeUv``: records every argv and imitates ``uv venv`` / ``uv pip install`` /
the venv's python, with switches to fail each step.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.services.update.os_slots import (
    SLOT_CONFIRMED,
    SLOT_EMPTY,
    SLOT_TRY_ONCE,
    SlotInfo,
    SlotStatus,
)
from tests.fakes.t7_bundles import read_fake_identity


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


@dataclass
class FakeOsSlots:
    """``OsSlotAdapter`` over an in-memory A/B model (stage0's rules)."""

    active: str = "A"
    slots: dict[str, SlotInfo] = field(default_factory=lambda: {
        "A": SlotInfo("A", "a" * 64, "1.0.0", SLOT_CONFIRMED), "B": SlotInfo("B")})
    bad_images: set[str] = field(default_factory=set)
    on_boot: Callable[[SlotInfo], None] | None = None     # the board comes up running this image
    reboot_fails: bool = False
    board_confirms: bool = True                            # harnessd confirms a healthy boot itself
    calls: list[str] = field(default_factory=list)
    armed: str = ""

    def status(self) -> SlotStatus:
        self.calls.append("status")
        return SlotStatus(active=self.active, slots=dict(self.slots))

    def write_inactive(self, image: Path, *, sha256: str, version: str, progress=None) -> str:
        target = next(n for n in sorted(self.slots) if n != self.active)
        self.calls.append(f"write:{target}")
        data = Path(image).read_bytes()
        import hashlib

        actual = hashlib.sha256(data).hexdigest()
        self.slots[target] = SlotInfo(target, actual, version, SLOT_EMPTY)
        if progress:
            progress("write", len(data), len(data))
        return target

    def arm_try_once(self, slot: str) -> None:
        self.calls.append(f"arm:{slot}")
        info = self.slots[slot]
        self.slots[slot] = SlotInfo(slot, info.image_sha256, info.version, SLOT_TRY_ONCE)
        self.armed = slot

    def reboot(self, progress=None, wait_s: float = 180.0) -> dict:
        self.calls.append("reboot")
        if self.reboot_fails:
            from harness_manager.core.errors import ActionFailedError

            raise ActionFailedError("reboot sent but no restart observed")
        booted = self.active
        if self.armed:
            cand = self.slots[self.armed]
            if cand.image_sha256 in self.bad_images:
                # The new image panicked before confirming: stage0 falls back.
                self.slots[self.armed] = SlotInfo(cand.name, cand.image_sha256, cand.version, "bad")
            else:
                booted = self.armed
                state = SLOT_CONFIRMED if self.board_confirms else SLOT_TRY_ONCE
                self.slots[booted] = SlotInfo(cand.name, cand.image_sha256, cand.version, state)
            self.armed = ""
        self.active = booted
        if self.on_boot:
            self.on_boot(self.slots[booted])
        if progress:
            for i, phase in enumerate(("sent", "down", "up"), 1):
                progress(phase, i, 3)
        return {"summary": f"booted slot {booted}", "slot": booted}

    def confirm(self, slot: str) -> None:
        self.calls.append(f"confirm:{slot}")
        info = self.slots[slot]
        if slot != self.active:
            from harness_manager.core.errors import ActionFailedError

            raise ActionFailedError(f"slot {slot} is not running")
        self.slots[slot] = SlotInfo(slot, info.image_sha256, info.version, SLOT_CONFIRMED)


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
