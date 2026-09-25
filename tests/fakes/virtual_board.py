"""VirtualMps3: one fake MPS3, assembled from the fakes, for integration tests.

Firmware profiles pin the fake to a real harness release. ``FIELDED_3F1A560F``
is what is on the lab board today: harness 1.0.0; the five features it
reports; ``reset`` accepts only ``dut`` (coordinator.c:246); USR_ACCESS
unreadable, so the build check is "unchecked". Features added by the harness
handover (stats, identify, reboot, log, mcc) belong in new profiles, added
when FakeShell grows them, never by editing this one.

The two generations that follow it (Team T12):

- ``FIELDED_ILA_MINT_BAKE`` / ``FIELDED_ILA_V011``: the 2026-10 ILA mint, on the
  lab board from 09-24. The card first carries the mint's own bake (the
  fielded firmware lineage re-keyed, USRACC now readable); v0.11 is trialled
  volatile and goes on the card only if it passes (W1 runbook §0.2).
- ``LINUX_HARNESSD``: ``mps3-harnessd`` on the MicroBlaze V, the product from
  mint 3 (~10-08 -> 10-12).

Their ``static_id``s are NOT FINAL (the ILA mint's is in the mint's own
``prod/static_id.txt``, not yet fielded; mint 3 has not been built). Each is a
placeholder that ``HARNESS_MANAGER_T12_ILA_STATIC_ID`` / ``HARNESS_MANAGER_T12_LINUX_STATIC_ID``
override; tests only rely on it differing from ``0x3F1A560F``.
"""

from __future__ import annotations

import inspect
import os
import random
import socket
import weakref
from dataclasses import dataclass, field, replace
from pathlib import Path

from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.transport import register_fake_serial, unregister_fake_serial

from .fake_identify import FakeIdentifyResponder
from .fake_mcc import FakeMcc
from .fake_sd import FakeSdVolume
from .t12_harness_shell import LINUX_OMITTED_DIAG_KEYS, HarnessFakeShell

#: The installed FakeShell takes ``identify_port`` (pyverify's Linux profile, 09-25 on).
_FAKESHELL_HAS_IDENTIFY = "identify_port" in inspect.signature(FakeShell.__init__).parameters

SHELL_0x3F1A560F = 0x3F1A560F

#: PLACEHOLDERS until the mints are fielded (see the module docstring).
ILA_MINT_STATIC_ID_PLACEHOLDER = 0x72BB0A36   # real: build_mint_2026_10/prod/static_id.txt (2026-09-23 14:56), not yet fielded
LINUX_MINT3_STATIC_ID_PLACEHOLDER = 0x11C30003


@dataclass(frozen=True)
class FirmwareProfile:
    name: str
    static_id: int
    harness_version: str
    harness_sha: str
    features: tuple[str, ...]
    reset_targets: tuple[str, ...]
    usr_access: int | None
    extra: dict = field(default_factory=dict)
    # --- T12 additions; the defaults are the bare-metal v0.10 shape of the fielded board
    harness_ver32: int = 0
    impl: str | None = None                 # version.impl: None = absent = bare-metal
    lmb_kb: int = 1024
    v011_verbs: bool = False                # stats / log / reboot answer
    identify: bool = False                  # UDP identify responder
    omit_diag_keys: tuple[str, ...] = ()    # diag keys this engine cannot fill (S4)
    version_extra: dict = field(default_factory=dict)   # additive version keys
    reboot_outage_s: float = 0.5            # the `reboot` verb's outage in the fake
    static_id_final: bool = True            # False while static_id is a placeholder


FIELDED_3F1A560F = FirmwareProfile(
    name="fielded-0x3F1A560F",
    static_id=SHELL_0x3F1A560F,
    harness_version="1.0.0",
    harness_sha="cb31b0f2",
    features=("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"),
    reset_targets=("dut",),
    usr_access=None,
)


def _static_id_from_env(var: str, placeholder: int) -> tuple[int, bool]:
    raw = os.environ.get(var, "").strip()
    return (int(raw, 0), True) if raw else (placeholder, False)


#: The v0.8 five, in the firmware's bit order (net-protocol.md "version").
_V08 = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")
#: PRODUCT=1 on a v0.11 image: bits 0-7 and 9-12 (net-protocol.md v0.11,
#: feat/rm-ila-mint d68dd0e; firmware/common/net_proto.c s_feature_names).
PRODUCT_V011_FEATURES = _V08 + ("dut_egress", "jtag_server", "xvc_dbgbr", "stats", "log",
                                "reboot", "touch_cal")
#: 1.0.0 packed: major<<24 | minor<<16 | patch<<8 | flags. VERSION on feat/rm-ila-mint
#: is still 1.0.0; the release may be tagged 1.1.0 (then pass harness_version/ver32).
_VER32_1_0_0 = 0x01000000


def ila_mint_bake_profile(static_id: int | None = None) -> FirmwareProfile:
    """What the card carries first on 09-24: the mint's OWN bake (stage 6 of the
    mint at c855108): the fielded firmware lineage re-keyed to the new static, so
    the five v0.8 features, but with USRACC fixed, so ``usr_access`` is readable
    and the skew check runs (W1 runbook §0.2, proof 3)."""
    sid, final = ((static_id, True) if static_id is not None else
                  _static_id_from_env("HARNESS_MANAGER_T12_ILA_STATIC_ID",
                                      ILA_MINT_STATIC_ID_PLACEHOLDER))
    return FirmwareProfile(
        name=f"ila-mint-bake-0x{sid:08X}", static_id=sid, harness_version="1.0.0",
        harness_sha="c8551081", features=_V08, reset_targets=("dut",),
        usr_access=_VER32_1_0_0, harness_ver32=_VER32_1_0_0, static_id_final=final)


def ila_v011_profile(static_id: int | None = None) -> FirmwareProfile:
    """The ILA mint's static with firmware v0.11 (d68dd0e, net-protocol v0.11):
    PRODUCT=1 features incl. ``stats``/``log``/``reboot``/``touch_cal``, USRACC
    readable (skew check runs), ``reset`` still ``dut`` only (A6 is not in v0.11),
    no ``impl`` key (bare-metal)."""
    base = ila_mint_bake_profile(static_id)
    return replace(base, name=f"ila-v011-0x{base.static_id:08X}", harness_sha="d68dd0ed",
                   features=PRODUCT_V011_FEATURES, v011_verbs=True)


def linux_harnessd_profile(static_id: int | None = None) -> FirmwareProfile:
    """``mps3-harnessd`` on the MicroBlaze V (mint 3). Agreed facts (plan §10/§10a):
    ``impl:"linux"``, ``lmb_kb`` 128, NO ``windowed`` (config_agent is built plain,
    the kernel paces TCP), the v0.11 verbs, identify on UDP 6899, diag keys it has
    no source for OMITTED, ``up_ms`` = process uptime + additive ``os_up_ms``, a
    reboot ~30-40 s longer than bare-metal (the pack's witness budget: 180 s)."""
    sid, final = ((static_id, True) if static_id is not None else
                  _static_id_from_env("HARNESS_MANAGER_T12_LINUX_STATIC_ID",
                                      LINUX_MINT3_STATIC_ID_PLACEHOLDER))
    return FirmwareProfile(
        name=f"linux-harnessd-0x{sid:08X}", static_id=sid, harness_version="1.0.0",
        harness_sha="unknown", features=tuple(f for f in PRODUCT_V011_FEATURES
                                               if f != "windowed"),
        reset_targets=("dut",), usr_access=_VER32_1_0_0, harness_ver32=_VER32_1_0_0,
        impl="linux", lmb_kb=128, v011_verbs=True, identify=True,
        omit_diag_keys=LINUX_OMITTED_DIAG_KEYS, reboot_outage_s=1.0, static_id_final=final)


FIELDED_ILA_MINT_BAKE = ila_mint_bake_profile()
FIELDED_ILA_V011 = ila_v011_profile()
LINUX_HARNESSD = linux_harnessd_profile()


#: Where a virtual board's ports come from: below every OS's ephemeral range (Linux 32768+,
#: Windows and macOS 49152+), so the kernel never hands one to another socket; clear of
#: ``held_gdb_block``'s 20000-23000, Harness Manager's own 23300-23727 and the HAPS tools'
#: local blocks (3200-3999, 24000+).
BOARD_PORT_RANGE = (10000, 20000)
SHELL_PORT_KEYS = ("control_port", "tftp_port", "raw_tcp_port", "uart0_port", "uart1_port",
                   "swo_port")
_UDP_SERVED = frozenset({"tftp_port", "identify_port"})


class BoardPorts:
    """The shell's ports, fixed for the board's life, so a REBOOT brings it back on them.

    A REBOOT closes every listener and the boot binds the same ports again. On ``bind(0)``
    ports that raced on a busy host: the kernel gave a port to another socket while the
    board was down, and the boot died with EADDRINUSE (FLAKE-2). These ports are outside
    the kernel's range, and each is held for the board's life with the protocol the shell
    does NOT serve on it (UDP beside a TCP port, TCP beside the TFTP port): the shell binds
    and rebinds freely, and no other ``BoardPorts``, in this process or another, can pick
    one meanwhile. ``keys``: what to claim (``identify_port``: the Linux harness's UDP
    identify responder, which a REBOOT restarts too). ``candidates``: the ports to pick
    from (tests of this class).
    """

    def __init__(self, candidates: tuple[int, ...] | None = None,
                 keys: tuple[str, ...] = SHELL_PORT_KEYS) -> None:
        self.ports: dict[str, int] = {}
        self._held: list[socket.socket] = []
        pool = candidates or range(*BOARD_PORT_RANGE)
        pick = random.SystemRandom()           # not `random`: pytest-randomly reseeds it
        try:
            for key in keys:
                self.ports[key] = self._claim(pool, pick, udp_served=key in _UDP_SERVED)
        except BaseException:
            self.release()
            raise

    def _claim(self, pool, pick, *, udp_served: bool) -> int:
        served_type, held_type = ((socket.SOCK_DGRAM, socket.SOCK_STREAM) if udp_served
                                  else (socket.SOCK_STREAM, socket.SOCK_DGRAM))
        for _ in range(500):
            port = pick.choice(pool)
            if port in self.ports.values():
                continue
            probe, held = socket.socket(type=served_type), socket.socket(type=held_type)
            try:
                probe.bind(("127.0.0.1", port))    # free for the shell (released below)
                held.bind(("127.0.0.1", port))     # and kept from every other claim
            except OSError:
                held.close()
                continue
            finally:
                probe.close()
            self._held.append(held)
            return port
        raise AssertionError(f"no free port for a virtual board among {len(pool)} candidates")

    @property
    def shell_ports(self) -> dict[str, int]:
        """The FakeShell's port keyword arguments."""
        return {k: v for k, v in self.ports.items() if k in SHELL_PORT_KEYS}

    @property
    def identify_port(self) -> int:
        return self.ports.get("identify_port", 0)

    def release(self) -> None:
        while self._held:
            self._held.pop().close()


def _needs_harness_fake(profile: FirmwareProfile) -> bool:
    return (profile.impl is not None or profile.v011_verbs or profile.identify
            or bool(profile.omit_diag_keys) or bool(profile.version_extra)
            or any(f not in _V08 for f in profile.features))


class VirtualMps3:
    """Start with ``with VirtualMps3(tmp_path) as vb:``; the parts are attributes."""

    def __init__(self, tmp_path: Path, profile: FirmwareProfile = FIELDED_3F1A560F,
                 *, boot_rm_id: int = 0, usb: bool = False, mode: str = "run",
                 unit: str | None = None) -> None:
        self.profile = profile
        common = dict(
            static_id=profile.static_id,
            boot_rm_id=boot_rm_id,
            reset_targets=profile.reset_targets,
            harness_version=profile.harness_version,
            harness_sha=profile.harness_sha,
            harness_usr_access=profile.usr_access,
            harness_ver32=profile.harness_ver32,
        )
        harness = _needs_harness_fake(profile) or mode != "run" or unit
        # The shell's ports, kept for the board's life (BoardPorts: a REBOOT rebinds them).
        self.board_ports = BoardPorts(
            keys=SHELL_PORT_KEYS + (("identify_port",) if harness else ()))
        self._release_ports = weakref.finalize(self, self.board_ports.release)
        common.update(self.board_ports.shell_ports)
        if harness:
            # T12: the v0.11 / Linux behaviours pyverify's FakeShell does not model yet.
            # The identify port stays 0 (no responder) unless the board runs one.
            responder = profile.identify or mode == "rescue"
            self.shell = HarnessFakeShell(
                "127.0.0.1", features=profile.features, impl=profile.impl, lmb_kb=profile.lmb_kb, v011_verbs=profile.v011_verbs,
                identify=profile.identify, omit_diag_keys=profile.omit_diag_keys,
                version_extra=profile.version_extra, reboot_outage_s=profile.reboot_outage_s,
                mode=mode, unit=unit,
                identify_port=self.board_ports.identify_port if responder else 0, **common)
        else:
            if _FAKESHELL_HAS_IDENTIFY:
                # pyverify's FakeShell defaults identify_port to the real 6899; this board
                # runs no responder, so it reports none (VirtualMps3.identify_port == 0).
                common["identify_port"] = 0
            self.shell = FakeShell("127.0.0.1", features=profile.features, **common)
        self._keepalive: FakeIdentifyResponder | None = None
        # boot_s=3 so a reboot is observable over Ethernet (the witness needs
        # two failed 1 s pings) when a test runs on the real clock.
        self.mcc = FakeMcc(on_reboot=self._on_reboot, on_boot=self._on_boot, boot_s=3.0)
        self.boot_rm_id = boot_rm_id
        self.boots = 0
        self.sd = FakeSdVolume(tmp_path / "sd")
        self.reboots = 0
        # usb=True registers the MCC as fake://<name> so the pack's USB adapters
        # (Team T3) can open it through harness_manager.core.transport.open_serial.
        self.usb = usb
        self.mcc_url = ""
        self._fake_name = f"mcc-{id(self):x}"

    def _on_reboot(self) -> None:
        # A real REBOOT power-cycles the board (proven 2026-08-04): the shell goes away.
        self.reboots += 1
        self.shell.stop()

    def _on_boot(self) -> None:
        # ...and the MCC reloads the base bitstream from the SD, so the shell comes back
        # on the same address with the boot design (greybox) loaded, not the last swap.
        self.boots += 1
        self.shell.current_rm_id = 0
        self.shell.start()

    @property
    def shell_endpoint(self) -> str:
        return f"{self.shell.host}:{self.shell.control_port}"

    @property
    def console_ports(self) -> dict[str, int]:
        return dict(getattr(self.shell, "console_ports", {}))

    # -- T12: harness states ------------------------------------------------------

    @property
    def identify_port(self) -> int:
        """The UDP identify port, 0 when this board has no responder."""
        return int(getattr(self.shell, "identify_port", 0) or 0)

    def identify_env(self) -> dict[str, str]:
        """The environment that points the pack's identify client at this board."""
        return {"HARNESS_MANAGER_MPS3_IDENTIFY_PORT": str(self.identify_port)}

    def hang(self) -> None:
        """mps3-harnessd hangs: 6900 still accepts, nothing replies, identify is silent."""
        self.shell.hung = True

    def unhang(self) -> None:
        self.shell.hung = False

    def set_busy(self, busy: bool = True) -> None:
        """Another client holds 6900 and the harness says so (A3 EBUSY)."""
        self.shell.busy = busy

    def kill_harness(self, *, keep_identify: bool = False) -> None:
        """mps3-harnessd dies: every TCP port refuses (the kernel is up). With
        ``keep_identify`` the identify responder stays (the "identify answers, the
        control port does not" diagnosis)."""
        port = self.identify_port
        self.shell.stop()
        if keep_identify and port:
            self._keepalive = FakeIdentifyResponder(self.shell.identify_reply, port=port).start()

    def revive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None
        self.shell.start()

    def enter_rescue(self, reason: str = "no bootable OS slot on the microSD") -> None:
        """stage0 RESCUE: identify answers ``mode:"rescue"``; nothing listens on TCP."""
        self.shell.stop()
        self.shell.mode = "rescue"
        self.shell.rescue_reason = reason
        if not getattr(self.shell, "identify_port", 0) and self.board_ports.identify_port:
            self.shell.identify_port = self.board_ports.identify_port   # the responder's, held
        self.shell.start()

    def leave_rescue(self) -> None:
        self.shell.stop()
        self.shell.mode = "run"
        self.shell.start()

    def candidate(self, *, ethernet: bool = True, usb: bool | None = None,
                  ssh: bool = False, tunnel: bool = False) -> Candidate:
        """A candidate with the links this virtual board exposes. ``ssh`` adds the
        Linux harness's SSH link; ``tunnel`` marks the Ethernet link as reached
        through a TCP-only tunnel (an ``ssh -L`` or a hub WSS tunnel)."""
        links: list[Link] = []
        if ethernet:
            detail = "shell control channel" + (" (via ssh tunnel)" if tunnel else "")
            links.append(Link(LinkKind.ETHERNET, self.shell_endpoint, detail))
        if ssh:
            links.append(Link(LinkKind.SSH, "ssh://root@127.0.0.1:22", "dropbear (fake)"))
        if self.usb if usb is None else usb:
            links.append(Link(LinkKind.USB_SERIAL, self.mcc_url, "MCC console (fake)"))
            links.append(Link(LinkKind.USB_MSD, str(self.sd.root), "V2M-MPS3 (fake)"))
        return Candidate(pack="mps3", board_id=f"mps3@{self.shell_endpoint}", links=tuple(links),
                         label="virtual MPS3", evidence="test fixture")

    def __enter__(self) -> VirtualMps3:
        self.shell.start()
        if self.usb:
            self.mcc_url = register_fake_serial(self._fake_name, self.mcc)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None
        self.shell.hung = False          # release any handler parked by hang()
        self.shell.stop()
        unregister_fake_serial(self._fake_name)
        self._release_ports()
