"""The Linux harness's claim lock on its board-only ports, for the CLAIMED-LOCK tests.

What harnessd does (platform ``feat/linux-harness`` at c99170b and later, read from its
code, not from a brief):

- ``firmware/xvc_server/xvc_server.h`` / ``jtag_server/jtag_server.h``: on a claimed board a
  peer that is not the board itself gets ONE line, then the close, checked at accept and
  BEFORE the single-client rule (``mps3_net_refuse_with_line``: drain what the peer sent,
  send the line, drain, close; ``firmware/common/net_if.c``);
- ``firmware/coordinator/coordinator.c`` ``claim_locked``: the D13 store's ``usd`` actions
  (format, clear, rescan) and the re-push ``commit`` answer ``<what> locked: board claimed
  (use ssh)`` with ``code`` "locked" before anything else is checked; ``usd`` status stays
  open;
- ``src/linux_harness/sw/harnessd/slot_linux.c`` ``peer_local``: "the board itself" is a
  127.0.0.0/8 peer (the far end of ``ssh -L ...:127.0.0.1:PORT``).

The fakes here stand "the board itself" in for ``TRUSTED`` (127.0.0.3), the address
``lxslots_board.BoardSsh`` relays its forwards from, the way pyverify's FakeShell takes a
``trusted_peer`` (harnessd's ``--mock-trusted-peer``). ``pin_claim`` makes a session's board
one THIS Harness Manager claimed (the pinned host key line and the ``claims.json`` record),
without running the TOFU claim.
"""

from __future__ import annotations

import socket
from typing import Any

from pyverify.testing.fakeshell import FakeShell

from tests.fakes.l1_fake_ssh import FakeSsh, FakeSshProcess
from tests.fakes.lc_fake_board_ssh import make_key_line

TRUSTED = "127.0.0.3"
XVC_LOCKED_LINE = (b'{"ok":false,"err":"xvc locked: board claimed (use ssh)",'
                   b'"code":"locked"}\n')
JTAG_LOCKED_LINE = (b'{"ok":false,"err":"jtag locked: board claimed (use ssh)",'
                    b'"code":"locked"}\n')
BOARD_KEY = make_key_line("the-claimed-board")
MY_KEY_FP = "SHA256:bXlrZXlteWtleW15a2V5bXlrZXlteWtleW15a2V5bXk"


def board_key_fp(key_line: str = BOARD_KEY) -> str:
    from harness_manager_mps3.claim import fingerprint

    return fingerprint(key_line.split()[1])


def refuse_with_line(conn: socket.socket, line: bytes) -> None:
    """``mps3_net_refuse_with_line``: drain what the peer sent, the line, drain, close."""
    def drain() -> None:
        conn.setblocking(False)
        try:
            for _ in range(64):
                if not conn.recv(64):
                    break
        except OSError:
            pass
        finally:
            conn.setblocking(True)

    drain()
    try:
        conn.sendall(line)
    except OSError:
        pass
    drain()
    conn.close()


def locked_peer(claimed: bool, peer_ip: str, trusted: str = TRUSTED) -> bool:
    """harnessd's rule: claimed, and the peer is not the board itself."""
    return claimed and peer_ip != trusted


def pin_claim(session: Any, key_line: str = BOARD_KEY) -> str:
    """Make ``session``'s board one THIS Harness Manager claimed: boards.toml
    ``boards.<b>.ssh.host_key`` is the key line and ``claims.json`` says the pin is ours.
    Returns the pinned fingerprint (the fake's identify must report it)."""
    from harness_manager_mps3 import claim as CL

    fp = board_key_fp(key_line)
    CL.write_ssh_settings(session.candidate, {"host_key": key_line})
    CL.ClaimRecords().update(session.candidate.board_id, by="you@test", key_fp=MY_KEY_FP,
                             at="2026-09-27T00:00:00Z", host_key_fp=fp, how="claim")
    return fp


#: The 6900 verbs board image v2.2 adds to the claim lock (display and touch_cal: some acts).
V22_LOCKED_VERBS = ("swap", "reset", "set_clk", "link", "macgen", "dutrx", "reboot")
PUSH_LOCKED_LINE = (b'{"ok":false,"err":"push locked: board claimed (use ssh)",'
                    b'"code":"locked"}\n')


def _gated_raw_push(handler: Any) -> Any:
    """6910 with the v2.2 lock: a non-local peer on a claimed ``claim_gate_all`` board gets ONE
    line and the close (``mps3_net_refuse_with_line``), whatever kind it pushes."""
    if getattr(handler, "_v22_gated", False):
        return handler

    class Gated(handler):                                 # type: ignore[misc, valid-type]
        _v22_gated = True

        def handle(self) -> None:
            shell = self.server.shell
            peer = self.client_address[0]
            if shell.claim_gate_all and shell.ssh_claimed and not shell._store_local(peer):
                shell.gated.append(("push", str(peer)))
                refuse_with_line(self.request, PUSH_LOCKED_LINE)
                return
            super().handle()

    return Gated


class StoreLock:
    """Mixin for a pyverify ``FakeShell``: the D13 store's claim lock (S6) as harnessd has
    it, with the slot lock's idea of "the board itself" (``slots.peer_local``: its
    ``trusted_peer``, else any 127.x peer), so a LAN test on 127.0.0.1 is never locked."""

    def _store_local(self, peer: str | None) -> bool:
        slots = getattr(self, "slots", None)
        if slots is not None:
            return bool(slots.peer_local(peer))
        return bool(peer) and str(peer).startswith("127.")

    #: Board image v2.2 (CLAIMROUTE): ``True`` also locks the state-changing verbs below, the
    #: design pushes on 6910 (kinds 0/1/2) and TFTP, for a non-local peer on a claimed board.
    #: Opt in per fake (``fake.claim_gate_all = True``); off, only the store/slot locks.
    claim_gate_all = False

    #: every verb the v2.2 lock refused: (op, peer)
    gated: list[tuple[str, str]]

    def _gate_all_refuses(self, request: dict[str, Any], peer: str | None) -> str:
        """The verb name to refuse, else "": v2.2's list. Reads (ping, version, stats, diag, log,
        telemetry, identity, locate, hello, panel, display query, slot/usd status) stay open."""
        op = str(request.get("op"))
        if op in V22_LOCKED_VERBS:
            return op
        if op == "display" and request.get("owner") != "query" and request.get("act") != "query":
            return op
        if op == "touch_cal" and request.get("act") in ("set", "default"):
            return op
        return ""

    def start(self) -> Any:
        super().start()                                   # type: ignore[misc]
        if not hasattr(self, "gated"):
            self.gated = []
        servers = getattr(self, "_servers", [])
        if len(servers) > 1:                              # [control, raw, ...]: gate 6910
            servers[1].RequestHandlerClass = _gated_raw_push(servers[1].RequestHandlerClass)
        return self

    def _tftp_receive(self, client: Any, wrq_packet: bytes) -> None:
        if isinstance(self, FakeShell) and self.claim_gate_all and self.ssh_claimed \
                and not self._store_local(client[0]):
            self.gated.append(("tftp", str(client[0])))
            self._tftp_send_error_from(None, client, 2, "access violation: board claimed (use ssh)")
            return
        super()._tftp_receive(client, wrq_packet)         # type: ignore[misc]

    def handle_control(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        op = request.get("op")
        if isinstance(self, FakeShell) and self.claim_gate_all and self.ssh_claimed \
                and not self._store_local(peer):
            verb = self._gate_all_refuses(request, peer)
            if verb:
                self.gated.append((verb, str(peer)))
                return {"ok": False, "err": f"{verb} locked: board claimed (use ssh)",
                        "code": "locked"}
        if isinstance(self, FakeShell) and self.ssh_claimed and not self._store_local(peer):
            if op == "usd" and request.get("action"):
                return {"ok": False, "err": "usd locked: board claimed (use ssh)",
                        "code": "locked"}
            if op == "commit":
                return {"ok": False, "err": "commit locked: board claimed (use ssh)",
                        "code": "locked"}
        return super().handle_control(request, peer=peer)  # type: ignore[misc]


# --- the lab through the hub, with the lock ------------------------------------------------------

BOARD_IP = "192.168.10.101"
HUB = "hub.claimed-lock.test"


class HubAndBoardSsh(FakeSsh):
    """One tunnel launcher for both SSH paths a session can take to the board:

    - the hub tunnel (``ssh HUB -L 127.0.0.1:p:BOARD_IP:PORT``): its relays leave from
      127.0.0.1, which is NOT the board itself, so a claimed board locks them out;
    - the claim forward (``ssh -J HUB -l root BOARD_IP -L 127.0.0.1:p:127.0.0.1:PORT``): its
      relays leave from ``TRUSTED``, the board's own loopback (``lxslots_board._FromBoard``).

    ``routes`` maps both kinds of far end (``(BOARD_IP, port)``, ``("127.0.0.1", port)``) to
    the fakes; nothing unrouted is ever dialled (``hub_loopback`` off)."""

    def __init__(self, board_ip: str = BOARD_IP, **kw: Any) -> None:
        kw.setdefault("hub_loopback", False)
        super().__init__(**kw)
        self.board_ip = board_ip
        self.source = TRUSTED

    def __call__(self, argv: Any) -> FakeSshProcess:
        from tests.fakes.lxslots_board import _FromBoard

        self.launches.append(list(argv))
        kind = _FromBoard if argv[-1] == self.board_ip else FakeSshProcess
        proc = kind(self, argv)
        self.procs.append(proc)
        return proc

    def board_launches(self) -> list[list[str]]:
        """The claim forwards launched (``-J HUB ... BOARD_IP``)."""
        return [a for a in self.launches if a[-1] == self.board_ip]


def forward_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-L"]


def route_board(ssh: HubAndBoardSsh, ports: dict[int, int]) -> None:
    """The board's real ports, as the hub sees them AND as its own loopback sees them."""
    for remote, local in ports.items():
        ssh.routes[(ssh.board_ip, remote)] = ("127.0.0.1", local)
        ssh.routes[("127.0.0.1", remote)] = ("127.0.0.1", local)


def observed_claimed(board_id: str, host_key_fp: str, *, claimed: bool = True) -> None:
    """claims.json's last identify check says the board is (not) claimed: what `board
    claim-status` leaves behind (through a hub ``info`` reads this, never the hub)."""
    import time

    from harness_manager_mps3 import claim as CL

    CL.ClaimRecords().update(board_id, observed={
        "claimed": claimed, "host_key": host_key_fp, "source": f"identify via {HUB}",
        "at": time.time()})
