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


#: The 6900 verbs board image v2.2 added to the claim lock (the lease fake gates on these).
#: The authority is ``pyverify.claim_table``; ``tests/integration/test_claim_real_table.py``
#: checks every one of these is a MUTATE row there.
V22_LOCKED_VERBS = ("swap", "reset", "set_clk", "link", "macgen", "dutrx", "reboot")


class StoreLock:
    """Mixin for a pyverify ``FakeShell``: records what the claim gate refused.

    The gate itself is the REAL one (rc2, platform 16a9aad6): ``FakeShell`` enforces
    ``pyverify.claim_table`` on 6900, the 6910 push header and the TFTP WRQ whenever
    ``ssh_claimed`` is set, for every peer that is not the board itself (``trusted_peer``,
    else 127/8). This mixin duplicates none of it. It only keeps ``gated``, the list of
    ``(verb, peer)`` the 6900 gate answered ``code == "locked"`` to, for the tests that assert
    "nobody was refused" / "refused exactly once"."""

    #: Only the ``lease`` verb's fake (``bl_board_lease.attach``) still reads this: the real
    #: table locks ``lease`` outright (lane SEC), which the lease lane is splitting into a READ
    #: row, so the lease fake keeps its own act-aware rule behind the flag.
    claim_gate_all = False

    #: every 6900 request the claim gate refused: (op, peer)
    gated: list[tuple[str, str]]

    def _store_local(self, peer: str | None) -> bool:
        slots = getattr(self, "slots", None)
        if slots is not None:
            return bool(slots.peer_local(peer))
        return bool(peer) and str(peer).startswith("127.")

    def start(self) -> Any:
        super().start()                                   # type: ignore[misc]
        if not hasattr(self, "gated"):
            self.gated = []
        return self

    def handle_control(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        reply = super().handle_control(request, peer=peer)  # type: ignore[misc]
        if isinstance(reply, dict) and reply.get("code") == "locked":
            if not hasattr(self, "gated"):
                self.gated = []
            self.gated.append((str(request.get("op")), str(peer)))
        return reply


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
