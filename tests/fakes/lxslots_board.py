"""A Linux-harness board for the LINUX-SLOTS tests: pyverify's FakeShell, extended.

``SlotBoard`` is ``FakeShell(profile="linux", slots=..., usd_card=...)`` (pyverify's own
model of harnessd's ``slot`` verb, the kind-2 push, the lock, ``usd`` and the re-push
``commit``) with ITS OWN model of what a ``reboot`` BOOTS: stage0's pick (the Linux lead's
S9): the default slot if it is valid and healthy, else the other slot if it is, else rescue
(``running`` "rescue"; the fake keeps answering 6900). A slot or image in ``unhealthy``
(slot names such as ``{"B"}``, or hdr_crcs) never comes up healthy, so stage0 falls back and
the DEFAULT STAYS on it. What this boot had read back is forgotten (``staged``, the
read-back, the job), and the harness reports the version the booted image carries
(``images``: hdr_crc -> {harness_version, harness_sha}). The vendored model's own reboot
(pyverify from platform 3f7cea2: slot names only) is replaced by this one, not run as well.

The harness the fake is (both knobs True = harnessd from platform 53f49b4 on, what the
vendored FakeShell models since 3f7cea2; False = an older image, for the twins):
``stamps_booted``: each confirmed healthy boot stamps its slot's record
(harnessd_slot_stamp_booted, HM_ANSWERS S1), so a slot ``stage0_mkcard.py`` wrote can be
verified (and rolled back to) after a reboot; ``refuses_reboot_in_job``: ``reboot`` answers
``EBUSY`` while a card job writes or verifies (HM change 6).

SLOT-TIMING's slow-job knobs (the card's speed, so a job lasts): ``write_bps`` receives a
push at that rate, counting ``job.got`` as the bytes reach the "card" (as harnessd does);
``verify_s`` keeps a read-back ``verifying`` that long; ``hold_job(...)`` sets a job that is
not this host's (another host's push, still writing) and ``end_job()`` finishes it. A restart
while a job writes or verifies WEDGES the card (``slot status`` answers ``card io``): what B2
saw on silicon ("uSD init error"), and the reason nothing may reset the board meanwhile (a
harness with ``refuses_reboot_in_job`` refuses its own ``reboot`` then; an MCC REBOOT or a
power cycle it cannot refuse).

The D13 store's claim lock (S6: ``usd`` actions and the re-push ``commit`` refused for a
peer that is not the board itself on a claimed board) is ``claimed_lock.StoreLock``'s.

``BoardSsh`` is L1's ``FakeSsh`` whose forwards reach the board FROM the board itself:
the relayed connection leaves from ``source`` (a FakeShell ``trusted_peer``), so the
claim lock lets it through while a direct connection from 127.0.0.1 is refused.

``board_session(fake)``: the real MPS3 pack opened on the fake's ephemeral ports.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from pyverify.testing.fakeshell import FakeShell, _recv_exactly

from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import StoreLock
from tests.fakes.l1_fake_ssh import FakeSsh, FakeSshProcess, _pipe

LINUX_SID = 0x72BB0A36
TRUSTED = "127.0.0.3"


class SlotBoard(StoreLock, FakeShell):
    def __init__(self, *args: Any, images: dict[int, dict[str, str]] | None = None,
                 unhealthy: set[int] | None = None, write_bps: float | None = None,
                 verify_s: float = 0.0, stamps_booted: bool = True,
                 refuses_reboot_in_job: bool = True, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.images = dict(images or {})
        self.unhealthy = set(unhealthy or ())
        self.boots: list[str] = []
        self.write_bps = write_bps
        self.verify_s = verify_s
        self.stamps_booted = stamps_booted
        self.refuses_reboot_in_job = refuses_reboot_in_job
        self.wedged = False
        self._verify_until = 0.0
        m = self.slots
        if m is not None:
            m.reboot = lambda: None          # stage0's pick is _simulate_restart's (module doc)
            verifying, poll = m._verifying, m.poll

            def timed_verifying(**result: Any) -> None:
                verifying(**result)
                self._verify_until = time.monotonic() + self.verify_s

            def timed_poll() -> None:
                if m.job["state"] == "verifying" and time.monotonic() < self._verify_until:
                    return
                poll()

            m._verifying = timed_verifying
            m.poll = timed_poll

    # -- SLOT-TIMING knobs ------------------------------------------------------------------

    def hold_job(self, state: str = "writing", *, act: str = "push", slot: str = "B",
                 got: int = 12_300_000, length: int = 29_000_000) -> None:
        """A card job this host did not start (another host's push): it stays until
        ``end_job``."""
        with self._lock:
            self.slots.job = {"act": act, "slot": slot, "state": state, "got": got,
                              "len": length, "err": ""}

    def end_job(self, state: str = "ok", err: str = "") -> None:
        with self._lock:
            self.slots.job.update(state=state, err=err)

    def _slot_push_tcp(self, sock: socket.socket, header: bytes, peer: str) -> None:
        if not self.write_bps:
            return super()._slot_push_tcp(sock, header, peer)
        with self._lock:
            verdict, slot, total = self.slots.push_begin(header, peer)
        if verdict != "go":
            _recv_exactly(sock, total + 1)
            return
        payload = bytearray()
        while len(payload) < total:
            try:
                chunk = sock.recv(min(4096, total - len(payload)))
            except OSError:
                break
            if not chunk:
                break
            payload += chunk
            with self._lock:
                self.slots.job["got"] = len(payload)     # what reached the card so far
            time.sleep(len(chunk) / self.write_bps)
        with self._lock:
            self.slots.push_end(slot, header, bytes(payload))

    def _op_reboot(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.refuses_reboot_in_job or self.slots is None:
            return super()._op_reboot(request)
        with self._lock:                   # an image before harnessd 53f49b4: no card-job EBUSY
            self.slots.busy = lambda: False
            try:
                return super()._op_reboot(request)
            finally:
                del self.slots.busy

    def _simulate_restart(self) -> None:
        super()._simulate_restart()
        m = self.slots
        if m is None:
            return
        with self._lock:
            if m.job["state"] in ("writing", "verifying"):
                self.wedged = True                         # B2: "uSD init error"
                m.card = "io"
            if self.stamps_booted and not self.wedged:
                m._stamp_running()         # the boot that ends stamped its slot once confirmed
            want = m.deflt
            sl = m.slot.get(want, {})
            ok = self._healthy(want, sl)
            booted = want if ok else self._fallback(want)
            m.running = booted
            m.boot_crc = int(m.slot.get(booted, {}).get("hdr_crc", 0) or 0)
            m._pending = None
            m.staged = None
            m.vcrc = {"A": 0, "B": 0}
            m.vsid = {"A": 0, "B": 0}
            m.job = {"act": "none", "slot": None, "state": "idle", "got": 0, "len": 0, "err": ""}
            m.confirmed = booted in ("A", "B")         # a healthy boot confirms (stage0's att)
            if self.stamps_booted and not self.wedged:
                m._stamp_running()                     # ... and stamps its slot's record (S1)
            ver = self.images.get(m.boot_crc)
            if ver:
                self.harness_version = ver.get("harness_version", self.harness_version)
                self.harness_sha = ver.get("harness_sha", self.harness_sha)
            self.boots.append(booted)

    # -- stage0's pick (S9) -------------------------------------------------------------------

    def _healthy(self, name: str, sl: dict[str, Any] | None = None) -> bool:
        sl = self.slots.slot.get(name, {}) if sl is None else sl
        return (sl.get("state") == "valid" and name not in self.unhealthy
                and sl.get("hdr_crc") not in self.unhealthy)

    def _fallback(self, want: str) -> str:
        other = "B" if want == "A" else "A"
        return other if self._healthy(other) else "rescue"


def slot_board(**kw: Any) -> SlotBoard:
    """A started Linux SlotBoard on ephemeral ports (defaults: slot A running, B empty,
    the fabric = ``LINUX_SID``, a fast reboot)."""
    kw.setdefault("profile", "linux")
    kw.setdefault("static_id", LINUX_SID)
    kw.setdefault("reboot_in_ms", 150)
    kw.setdefault("slots", {})
    kw.setdefault("harness_version", "1.0.0")
    fake = SlotBoard.ephemeral(**kw)
    fake.start()
    return fake


def board_session(fake: FakeShell, *, poll_s: float = 0.02) -> Any:
    """The real MPS3 pack's session on ``fake`` (its slot adapter polls fast)."""
    pack = Mps3Pack(console_ports=fake.console_ports, push_port=fake.raw_tcp_port,
                    tftp_port=fake.tftp_port)
    cand = pack.candidate_for_host(f"{fake.host}:{fake.control_port}")
    session = pack.open(cand)
    if session.os_slots is not None:
        session.os_slots.poll_s = poll_s
        session.os_slots.poll_max_s = poll_s
        session.os_slots.reboot_poll_s = poll_s
    return session


class _FromBoard(FakeSshProcess):
    def _serve(self, listener: socket.socket, rhost: str, rport: int) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            self.accepted += 1
            target = self.owner.route(rhost, rport)
            try:
                if target is None:
                    raise ConnectionRefusedError("no route in the fake")
                upstream = socket.create_connection(target, timeout=2.0,
                                                    source_address=(self.owner.source, 0))
                upstream.settimeout(None)
            except OSError:
                self.channel_failures += 1
                line = "channel 2: open failed: connect failed: Connection refused"
                self.stderr_tail += line + "\n"
                self.open_failures.append((time.monotonic(), line))
                client.close()
                continue
            with self._mu:
                self._conns += [client, upstream]
            done = threading.Event()
            threading.Thread(target=_pipe, args=(client, upstream, done), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client, done), daemon=True).start()


class BoardSsh(FakeSsh):
    """``ssh -J HUB root@BOARD -L ...:127.0.0.1:6900`` as the board sees it: from itself."""

    def __init__(self, fake: FakeShell, *, source: str = TRUSTED, **kw: Any) -> None:
        super().__init__(**kw)
        self.source = source
        self.routes[("127.0.0.1", 6900)] = (fake.host, fake.control_port)
        self.routes[("127.0.0.1", 6910)] = (fake.host, fake.raw_tcp_port)

    def __call__(self, argv: Any) -> FakeSshProcess:
        self.launches.append(list(argv))
        proc = _FromBoard(self, argv)
        self.procs.append(proc)
        return proc
