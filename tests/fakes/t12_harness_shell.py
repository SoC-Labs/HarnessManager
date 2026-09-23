"""``HarnessFakeShell``: pyverify's FakeShell plus the two harness generations of 09-24..10-12.

What it adds, each cited to the agreement it models (Linux harness plan §10/§10a,
net-protocol.md v0.11 on ``feat/rm-ila-mint``):

- ``version``: the v0.11 feature names (the installed FakeShell validates against
  the five v0.8 names only), ``impl`` (ADDITIVE; absent = bare-metal) and any
  additive ``version_extra`` keys (``proto``, ``usercode``, ``clr_max``,
  ``reset_targets``) a future harness may send (``stats_extra`` likewise);
- the v0.11 verbs ``stats`` (``up_ms`` = PROCESS uptime, ``os_up_ms`` additive on
  Linux), ``log`` and ``reboot`` (reply first, then an OUTAGE: every port closed
  for ``reboot_outage_s``, then back with ``up_ms`` reset and the boot-default RM);
- ``diag`` OMITTING the keys ``mps3-harnessd`` has no source for (S4);
- ``hung``: 6900 still ACCEPTS (the kernel's backlog) but never replies, and
  identify is silent (the same service loop);
- ``busy``: every request answers ``{"ok":false,"err":"EBUSY",...}`` (A3);
- ``mode="rescue"``: stage0 rescue: identify answers ``mode:"rescue"`` + ``reason``,
  and NOTHING listens on 6900/6910/6930-6932;
- the UDP identify responder (``tests.fakes.fake_identify``).

UPSTREAM FIRST. pyverify's HOST lane is adding ``FakeShell(profile="linux",
hung=, mode=, identify_port=, ...)``. When the installed FakeShell accepts those
keywords, this class passes them through and does NOT install its own
version/diag/hung/identify/rescue behaviour (``self.upstream`` is True).
``HARNESS_MANAGER_T12_FAKES=local`` forces the local models, ``=upstream`` requires
the upstream ones. EBUSY, the reboot outage and ``version_extra`` stay local:
upstream does not model them.
"""

from __future__ import annotations

import inspect
import os
import socket
import threading
import time
from typing import Any

from pyverify.testing.fakeshell import FakeShell

from .fake_identify import FakeIdentifyResponder

#: The FakeShell keywords the upstream Linux profile adds (pyverify HOST lane).
_UPSTREAM_KEYS = frozenset({"profile", "hung", "mode", "identify", "identify_port", "unit",
                            "os_boot_ms", "omit_diag_keys", "v011_verbs"})
UPSTREAM_AVAILABLE = _UPSTREAM_KEYS <= set(inspect.signature(FakeShell.__init__).parameters)


def use_upstream() -> bool:
    want = os.environ.get("HARNESS_MANAGER_T12_FAKES", "auto").strip().lower()
    if want == "upstream" and not UPSTREAM_AVAILABLE:
        raise RuntimeError("HARNESS_MANAGER_T12_FAKES=upstream but the installed pyverify FakeShell "
                           "has no linux profile")
    return UPSTREAM_AVAILABLE and want != "local"


#: HARNESSD_CONTRACT §5.4 (provisional, as in the upstream fake): diag keys
#: mps3-harnessd has no source for (no lwIP pcb, no bare-metal LAN9220 driver).
LINUX_OMITTED_DIAG_KEYS = ("rx_recover", "rx_dumps", "rx_queued", "pbuf_free",
                           "tx_status_drained", "tx_fifo_full_drops", "tx_space_stalls",
                           "tx_iface_errors", "tx_last_status")


def _hex32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


def _free_tcp_port(host: str) -> int:
    """A port nothing listens on (a connect is refused): bind, read, release."""
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class HarnessFakeShell(FakeShell):
    def __init__(self, host: str = "127.0.0.1", *, impl: str | None = None,
                 features: tuple[str, ...] = (), lmb_kb: int = 1024,
                 v011_verbs: bool = False, omit_diag_keys: tuple[str, ...] = (),
                 identify: bool = False, identify_port: int = 0, unit: str | None = None,
                 mode: str = "run", rescue_reason: str = "no bootable OS slot on the microSD",
                 hung: bool = False, os_boot_ms: int = 18000,
                 reboot_in_ms: int = 100, reboot_outage_s: float = 0.5,
                 version_extra: dict[str, Any] | None = None,
                 stats_extra: dict[str, Any] | None = None,
                 ssh_claimed: bool = False, ssh_host_key_sha256: str = "SHA256:fake-host-key",
                 mac: str = "02004d505300", dhcp: bool = False, **kwargs: Any) -> None:
        self.upstream = use_upstream()
        if self.upstream:
            super().__init__(
                host, profile="linux" if impl == "linux" else "bare-metal", impl=impl,
                features=features, lmb_kb=lmb_kb, v011_verbs=v011_verbs,
                omit_diag_keys=omit_diag_keys, identify=identify, identify_port=identify_port,
                unit=unit, mode=mode, hung=hung, os_boot_ms=os_boot_ms,
                ssh_claimed=ssh_claimed, ssh_host_key_sha256=ssh_host_key_sha256,
                mac=mac, dhcp=dhcp, board_ip=host, reboot_in_ms=reboot_in_ms, **kwargs)
        else:
            super().__init__(host, lmb_kb=lmb_kb, **kwargs)
            # The installed codec knows the five v0.8 names; the firmware's canonical
            # order is kept by the caller (profiles list them in bit order).
            self.features = tuple(features)
            self.impl = impl
            self.v011_verbs = v011_verbs
            self.omit_diag_keys = frozenset(omit_diag_keys)
            self.identify_enabled = identify
            self.identify_port = identify_port
            self.unit = unit
            self.mode = mode
            self.hung = hung
            self.os_boot_ms = os_boot_ms
            self.reboot_in_ms = reboot_in_ms
            self.ssh_claimed = ssh_claimed
            self.ssh_host_key_sha256 = ssh_host_key_sha256
            self.mac = mac
            self.dhcp = dhcp
            self._t0 = time.monotonic()
            self.log_ring = bytearray(b"--- MPS3 nanoSoC shell firmware (A3) starting ---\r\n")
            #: every identify datagram seen: (sender, request or None, replied?), as upstream
            self.identify_requests: list = []
        self.busy = False
        self.rescue_reason = rescue_reason
        self.reboot_outage_s = reboot_outage_s
        self.version_extra = dict(version_extra or {})
        self.stats_extra = dict(stats_extra or {})
        #: reboots served by the `reboot` verb (the outage model), in order
        self.verb_reboots: list[float] = []
        self._responder: FakeIdentifyResponder | None = None

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> HarnessFakeShell:
        if self.upstream:
            super().start()
            return self
        if self.mode == "rescue":
            # stage0 rescue: no TCP service at all. Keep the control port a port that
            # REFUSES, so a client aimed at it sees nothing there.
            if not self.control_port:
                self.control_port = _free_tcp_port(self.host)
        else:
            super().start()
        if self.identify_enabled or self.mode == "rescue":
            self._start_responder()
        return self

    def stop(self) -> None:
        if not self.upstream:
            self._stop_responder()
        super().stop()

    def _start_responder(self) -> None:
        if self._responder is None:
            self._responder = FakeIdentifyResponder(
                self._identify_or_silent, host=self.host, port=self.identify_port,
                requests=self.identify_requests).start()
            self.identify_port = self._responder.port

    def _stop_responder(self) -> None:
        if self._responder is not None:
            self._responder.stop()
            self._responder = None

    # -- clocks ---------------------------------------------------------------------

    def up_ms(self) -> int:
        if self.upstream:
            return super().up_ms()
        return int((time.monotonic() - self._t0) * 1000) & 0xFFFFFFFF

    def os_up_ms(self) -> int:
        return (self.os_boot_ms + self.up_ms()) & 0xFFFFFFFF

    # -- identify -------------------------------------------------------------------

    def _identify_or_silent(self, nonce: str) -> dict[str, Any] | None:
        return None if self.hung else self.identify_reply(nonce)

    def identify_reply(self, nonce: str) -> dict[str, Any]:   # same name as upstream's
        if self.upstream:
            return super().identify_reply(nonce)            # type: ignore[misc]
        rescue = self.mode == "rescue"
        reply: dict[str, Any] = {
            "ok": True, "op": "identify", "v": 1, "nonce": nonce, "board": "mps3",
            "mac": self.mac, "ip": self.host, "dhcp": self.dhcp,
            "shell_id": _hex32(self.static_id),
            "rm_id": _hex32(0 if rescue else self.current_rm_id),
            "harness": "" if rescue else self.harness_version, "proto": "0.11",
        }
        if self.impl and not rescue:
            reply["impl"] = self.impl
        if self.unit:
            reply["unit"] = self.unit
        reply["up_ms"] = self.up_ms()
        if self.impl and not rescue:
            reply["os_up_ms"] = self.os_up_ms()
        reply["mode"] = "rescue" if rescue else "run"
        if rescue and self.rescue_reason:
            reply["reason"] = self.rescue_reason
        if self.impl and not rescue:
            reply["ssh"] = {"claimed": self.ssh_claimed,
                            "host_key_sha256": self.ssh_host_key_sha256}
        reply["ports"] = ({"tftp": self.tftp_port} if rescue else {
            "ctrl": self.control_port, "push": self.raw_tcp_port, "tftp": self.tftp_port,
            "jtag": 6921, "xvc": 2542, "uart0": self.uart0_port, "uart1": self.uart1_port,
            "swo": self.swo_port})
        return reply

    # -- 6900 -----------------------------------------------------------------------

    def handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.upstream:
            # HUNG: the kernel accepted the connection; nothing reads it until the hang
            # clears (then the buffered request is served, like a resumed process).
            while self.hung and not self._stop.is_set():
                time.sleep(0.02)
        if self.busy:
            return {"ok": False, "err": "EBUSY", "since_ms": 1500, "holder": "10.0.0.7:51234"}
        op = request.get("op")
        if op == "reboot" and self.v011_verbs:
            return self._op_reboot_outage(request)
        if not self.upstream and self.v011_verbs and op in ("stats", "log"):
            return getattr(self, f"_op_{op}")(request)
        return super().handle_control(request)

    def _op_version(self, request: dict[str, Any]) -> dict[str, Any]:
        reply = super()._op_version(request)
        if not self.upstream and self.impl:
            reply["impl"] = self.impl
        reply.update(self.version_extra)
        return reply

    def _op_diag(self, request: dict[str, Any]) -> dict[str, Any]:
        reply = super()._op_diag(request)
        if not self.upstream:
            for key in self.omit_diag_keys:
                reply.pop(key, None)
        return reply

    def _op_stats(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            last = self.swaps[-1] if self.swaps else None
            reply: dict[str, Any] = {
                "ok": True, "up_ms": self.up_ms(), "sid": _hex32(self.static_id),
                "rm": _hex32(self.current_rm_id), "rm_ok": bool(self.rm_id_valid),
                "lock": False, "clk_sel": 0, "mmcm": True, "clk_alive": True,
                "dut_rst": True, "rp_rst": True, "decpl": False, "link": bool(self.link_up),
                "spd": 100, "fdx": True, "mac": self.mac,
                "swap": "await_partial" if self._swap_in_flight else "idle",
                "swap_ok": bool(last and last.get("final") == "DONE"),
                "swap_n": len(self.swaps), "icap": 0, "rxdrop": 0, "txerr": 0,
                "swap_err": "", "clr_ok": True, "dut_mhz": 50, "svc_max_us": 0,
                "svc_skipped": 0,
            }
            if self.impl:
                reply["os_up_ms"] = self.os_up_ms()
            reply.update(self.stats_extra)
            return reply

    def _op_log(self, request: dict[str, Any]) -> dict[str, Any]:
        off = request.get("off", 0)
        if not isinstance(off, int) or isinstance(off, bool) or off < 0:
            return {"ok": False, "err": "bad args"}
        chunk = bytes(self.log_ring[off:off + 256])
        return {"ok": True, "off": min(off, len(self.log_ring)), "n": len(chunk),
                "more": off + len(chunk) < len(self.log_ring), "dropped": 0,
                "data": chunk.hex()}

    def _op_reboot_outage(self, request: dict[str, Any]) -> dict[str, Any]:
        """net-protocol v0.11 ``reboot``: the reply goes FIRST, then the shell restarts.
        Refused mid-swap with EBUSY. The outage is modelled: every port closes for
        ``reboot_outage_s`` (Linux: a full OS boot), then the shell is back with
        ``up_ms`` reset and the boot-default overlay (greybox) resident."""
        with self._lock:
            if self._swap_in_flight:
                return {"ok": False, "err": "EBUSY"}
            self.verb_reboots.append(time.monotonic())
        threading.Thread(target=self._restart_after_reply, daemon=True).start()
        return {"ok": True, "in_ms": self.reboot_in_ms}

    def _restart_after_reply(self) -> None:
        time.sleep(self.reboot_in_ms / 1000.0)
        self.stop()
        time.sleep(self.reboot_outage_s)
        self.current_rm_id = 0
        self._t0 = time.monotonic()
        self.start()
