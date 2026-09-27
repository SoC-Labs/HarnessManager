"""FakeLcdMirror: the board's ``mps3-lcdmirror`` service on an ephemeral loopback port (lane LM1).

Lifted from the transport spike's board side (``tests/spikes/lcd_mirror_transport.py``
``FakeBoard``) and brought up to the agreed wire (``docs/design/LCD_MIRROR.md`` §6.1) with
H1 as the Linux lead is building it (``lcdmirror_main.c``, uncommitted):

- at most ``clients_max`` (2) clients; the next one gets ONE refusal line, then close;
- HELLO first (``proto, w, h, fmt, tile, mode, static_id, max_msg``, plus H3's
  ``boot_id, rate, rate_max, clients_max``); nothing until the client's KEY; ``seq`` 1 for
  the first UPDATE of a connection, +1 each (the board's §6.1 correction);
- per client, at most ``rate`` SNAPs a second, and only while it has room: at most
  ``window`` UPDATEs unACKed (H1; ``window=None`` models a board without flow control);
- a SNAP carries the tiles that CHANGED against what this client holds, plus tiles that
  became VALID again; a keyframe (the first SNAP after KEY) every VALID tile, with REGS on
  its first part. Every tile in its smallest encoding, cut into UPDATEs of at most
  ``max_msg`` (header included, H3), all with one t_ms/frames; the last carries
  ``snap_last`` (H1; ``snap_last=False`` models a board without it);
- nothing changed: no UPDATE, ever (no heartbeats: PING is the liveness probe); ``RATE``
  clamps to 0..``rate_max`` (0 = pause) and the clamp is the board's own ``0x11`` reply;
  PING -> PONG at any time; owner 0/1 only (3 is reserved);
- status: the CSR bits, then ``exact`` (hw, no viol, fmt_ok, no approx) or ``text_only``
  (sw) and ``blind`` (sw while the DUT owns the panel).

Scenarios (the design's §7.2 table): ``refuse`` a third client (automatic), ``gap(n)``
(the next n UPDATEs are lost, seq still advances), ``stop_service``/``start_service``
(no listener: an image without the service), ``drop()`` (every connection closes: a tunnel
drop or a board reboot), ``block(exc)``/``unblock()`` (the connector raises: a claim lost,
then reclaimed), ``handover(owner)`` (RESETS+1, VALID all 0, the new owner repaints),
sw-blind (``mode="sw"`` + the DUT owns the panel), and the RATE clamp.

The picture (``FakePanel``) is mutated by tests under ``fake.lock`` (``with fake.edit() as
p:``), or by an ``animate(panel, now)`` callable run at every SNAP. Every port is
127.0.0.1:0, re-picked while the kernel's choice is in a forbidden range.
"""

from __future__ import annotations

import contextlib
import os
import select
import socket
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from typing import Any

from harness_manager.core import display_wire as w

FORBIDDEN = ((23300, 23727), (10000, 19999))
STATIC_ID = "0x44EE76D5"


def bind_ephemeral() -> socket.socket:
    """127.0.0.1:0, re-picked while the kernel's choice is in a forbidden range."""
    for _ in range(64):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        if not any(lo <= port <= hi for lo, hi in FORBIDDEN):
            return s
        s.close()
    raise RuntimeError("no ephemeral port outside the forbidden ranges")


def baseline_regs() -> bytes:
    """REGS as the harness init table leaves them (hx8347_init.c; LCD_MIRROR_FPGA.md §1.4)."""
    r = bytearray(w.REGS_SIZE)
    for k, v in {0x01: 0x00, 0x16: 0x20, 0x17: 0x05, 0x1F: 0x90, 0x28: 0x3C, 0x36: 0x09,
                 0x04: 0x01, 0x05: 0x3F, 0x08: 0x00, 0x09: 0xEF}.items():
        r[k] = v
    return bytes(r)


def crc_valid(frame: bytes | bytearray, valid: set[int] | frozenset[int]) -> int:
    """CRC over the VALID tiles only (an invalid tile's pixels are unknown by definition)."""
    c = 0
    for t in sorted(valid):
        c = zlib.crc32(w.tile_of_frame(frame, t), c)
    return c


class FakePanel:
    """What the fake board serves: the picture in viewer order (row-major RGB565 LE), VALID,
    the counters, the owner, the CSR status bits and the 256 registers."""

    def __init__(self, frame: bytes | None = None, *, owner: int = w.OWNER_HARNESS,
                 valid: set[int] | None = None, regs: bytes | None = None) -> None:
        self.gram = bytearray(frame if frame is not None else bytes(w.FRAME_BYTES))
        self.valid: set[int] = set(range(w.NTILES)) if valid is None else set(valid)
        self.frames = 0
        self.resets = 0
        self.owner = owner
        self.regs = bytearray(regs if regs is not None else baseline_regs())
        self.csr = w.ST_RST_N | w.ST_BL | w.ST_DISPLAY_ON | w.ST_FMT_OK
        self.blind = False                    # sw mode: the model knows the DUT owns the panel

    def set_frame(self, frame: bytes, *, valid: set[int] | None = None) -> None:
        self.gram[:] = frame
        if valid is not None:
            self.valid = set(valid)
        self.frames += 1

    def put_tile(self, t: int, px: bytes) -> None:
        w.put_tile_in_frame(self.gram, t, px)

    def fill_tile(self, t: int, colour: int) -> None:
        self.put_tile(t, colour.to_bytes(2, "little") * w.TILE_PX)
        self.valid.add(t)

    def status_word(self, mode: str) -> int:
        st = self.csr & w.ST_CSR_MASK & ~w.ST_OWNER
        if self.owner == w.OWNER_DUT:
            st |= w.ST_OWNER
        if mode == "hw":
            if not st & w.ST_VIOL and st & w.ST_FMT_OK and not st & w.ST_APPROX:
                st |= w.S_EXACT
        else:
            st |= w.S_TEXT_ONLY
            if self.blind or self.owner == w.OWNER_DUT:
                st |= w.S_BLIND
        return st


class _Client:
    def __init__(self, fake: FakeLcdMirror, conn: socket.socket, no: int) -> None:
        self.fake = fake
        self.conn = conn
        self.no = no
        self.started = False
        self.key_req = False
        self.rate = fake.rate_default
        self.next_snap = 0.0
        self.seq = fake.seq_base & 0xFFFFFFFF          # per connection: the first UPDATE is
        self.acked = self.seq                          # seq_base + 1 = 1 (ACK 0 acks nothing)
        self.parts: list[tuple[list[bytes], int]] = []           # (records, flags) to send
        self.snap_hdr: tuple[Any, ...] | None = None
        self.held = bytearray(w.FRAME_BYTES)                      # what this client holds
        self.held_valid: set[int] = set()
        self.sig: tuple[Any, ...] | None = None
        self.max_unacked = 0
        self.alive = True

    def room(self) -> bool:
        win = self.fake.window
        return win is None or ((self.seq - self.acked) & 0xFFFFFFFF) < win


class FakeLcdMirror:
    """The board's lcd_mirror service, on 127.0.0.1:<ephemeral>. See the module doc."""

    def __init__(self, panel: FakePanel | None = None, *, mode: str = "hw",
                 max_msg: int = w.MAX_MSG_DEFAULT, rate_max: int = 30, rate_default: int = 5,
                 window: int | None = w.ACK_WINDOW, clients_max: int = 2, snap_last: bool = True,
                 static_id: str = STATIC_ID, boot_id: str = "fake-boot-1",
                 animate: Callable[[FakePanel, float], None] | None = None,
                 poll_s: float = 0.005, seq_base: int = 0) -> None:
        self.panel = panel or FakePanel()
        self.mode = mode
        self.max_msg = max_msg
        self.rate_max = rate_max
        self.rate_default = rate_default
        self.window = window
        self.clients_max = clients_max
        self.snap_last = snap_last
        self.static_id = static_id
        self.boot_id = boot_id
        self.animate = animate
        self.poll_s = poll_s
        #: The board starts every connection at seq 1 (seq_base 0). A test sets it near 2^32
        #: to make a long-lived connection's wrap happen at once.
        self.seq_base = seq_base
        self.lock = threading.RLock()
        self._clients: list[_Client] = []
        self._conn_no = 0
        self._gap = 0
        self._blocked: BaseException | None = None
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self.port = 0
        self._enc_cache: dict[bytes, tuple[int, bytes]] = {}
        self.stats: dict[str, Any] = {
            "connects": 0, "refused": 0, "updates": 0, "bytes": 0, "keys": 0, "key_updates": 0,
            "snaps": 0, "gaps": 0, "pongs": 0, "acks": 0, "rate": rate_default,
            "rate_asked": None, "window_waits": 0, "max_unacked": 0, "tiles": 0,
            "enc_hist": [0] * 5, "snap_parts_max": 0}

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> FakeLcdMirror:
        self.start_service()
        return self

    def start_service(self) -> None:
        """Listen (again, on the same port once one was picked)."""
        if self._srv is not None:
            return
        if self.port:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.port))
        else:
            s = bind_ephemeral()
            self.port = s.getsockname()[1]
        s.listen(8)
        self._srv = s
        self._accept_thread = threading.Thread(target=self._accept, args=(s,), daemon=True,
                                               name=f"fake-lcdm-accept-{self.port}")
        self._accept_thread.start()

    def stop_service(self) -> None:
        """Stop listening (connects are refused: an image without the service) and close
        every connection."""
        s, self._srv = self._srv, None
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
        self.drop()

    def close(self) -> None:
        self._stop.set()
        self.stop_service()

    def __enter__(self) -> FakeLcdMirror:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the connector (what LM2's claim.open_forward stands in for) --------------------------

    def connect(self) -> socket.socket:
        """A NEW connection to the service (the product: a socket to the SSH forward)."""
        blocked = self._blocked
        if blocked is not None:
            raise blocked
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def block(self, exc: BaseException) -> None:
        """Every ``connect()`` raises ``exc`` (a claim lost, a key refused) and live
        connections close."""
        self._blocked = exc
        self.drop()

    def unblock(self) -> None:
        self._blocked = None

    # -- scenarios ------------------------------------------------------------------------------

    def drop(self) -> None:
        """Every connection closes at once (the SSH tunnel dropped; the board rebooted)."""
        with self.lock:
            clients = list(self._clients)
        for c in clients:
            c.alive = False
            with contextlib.suppress(OSError):
                c.conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                c.conn.close()

    def gap(self, n: int = 1) -> None:
        """The next ``n`` non-key UPDATEs are lost on the way (seq still advances)."""
        with self.lock:
            self._gap += n

    @contextlib.contextmanager
    def edit(self) -> Iterator[FakePanel]:
        """Change the picture atomically (a SNAP never sees half of it)."""
        with self.lock:
            yield self.panel

    def handover(self, owner: int) -> None:
        """A KVM handover: CLCD_RST pulses (RESETS+1), VALID goes all 0, the new owner
        repaints (the test paints, tile by tile or at once)."""
        with self.lock:
            self.panel.resets += 1
            self.panel.valid = set()
            self.panel.owner = owner

    # -- observation ----------------------------------------------------------------------------

    @property
    def clients(self) -> int:
        with self.lock:
            return sum(1 for c in self._clients if c.alive)

    def client_seqs(self) -> list[int]:
        with self.lock:
            return [c.seq for c in self._clients if c.alive]

    def idle(self) -> bool:
        """Every client has sent all it will send for the current picture: no parts queued,
        nothing changed against what it holds, and its window is clear."""
        with self.lock:
            p = self.panel
            for c in self._clients:
                if not c.alive or not c.started:
                    continue
                if c.parts or c.key_req or c.held_valid != p.valid:
                    return False
                if any(w.tile_of_frame(c.held, t) != w.tile_of_frame(p.gram, t) for t in p.valid):
                    return False
                if c.seq != c.acked and self.window is not None:
                    return False
            return True

    def picture(self) -> tuple[bytes, set[int]]:
        with self.lock:
            return bytes(self.panel.gram), set(self.panel.valid)

    # -- the service ----------------------------------------------------------------------------

    def hello(self) -> w.DisplayInfo:
        return w.DisplayInfo(mode=self.mode, static_id=self.static_id, max_msg=self.max_msg,
                             extra={"boot_id": self.boot_id, "rate": self.rate_default,
                                    "rate_max": self.rate_max, "clients_max": self.clients_max})

    def _accept(self, srv: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with self.lock:
                full = sum(1 for c in self._clients if c.alive) >= self.clients_max
                if not full:
                    self._conn_no += 1
                    client = _Client(self, conn, self._conn_no)
                    self._clients.append(client)
                    self.stats["connects"] += 1
            if full:
                self.stats["refused"] += 1
                with contextlib.suppress(OSError):
                    conn.sendall(w.refusal_line(f"lcd_mirror: busy ({self.clients_max} clients)"))
                with contextlib.suppress(OSError):
                    conn.close()
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True,
                             name=f"fake-lcdm-conn{client.no}").start()

    def _serve(self, c: _Client) -> None:
        try:
            c.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.conn.sendall(w.hello_msg(self.hello()))
            rbuf = bytearray()
            while c.alive and not self._stop.is_set():
                r, _, _ = select.select([c.conn], [], [], self.poll_s)
                if r:
                    data = c.conn.recv(65536)
                    if not data:
                        return
                    rbuf += data
                    self._control(c, rbuf)
                self._maybe_update(c)
        except OSError:
            pass
        finally:
            c.alive = False
            with contextlib.suppress(OSError):
                c.conn.close()
            with self.lock:
                if c in self._clients:
                    self._clients.remove(c)

    def _send(self, c: _Client, m: bytes) -> None:
        c.conn.sendall(m)
        self.stats["bytes"] += len(m)

    def _control(self, c: _Client, rbuf: bytearray) -> None:
        while len(rbuf) >= w.HEADER_SIZE:
            magic, typ, _r, ln = w.HEADER.unpack_from(rbuf)
            if magic != w.MAGIC or ln > 64:
                raise OSError("not this protocol: drop the client")
            if len(rbuf) < w.HEADER_SIZE + ln:
                return
            body = bytes(rbuf[w.HEADER_SIZE:w.HEADER_SIZE + ln])
            del rbuf[:w.HEADER_SIZE + ln]
            if typ == w.T_KEY:
                c.key_req = c.started = True
            elif typ == w.T_RATE and ln >= 1:
                self.stats["rate_asked"] = body[0]
                hz = min(self.rate_max, body[0])
                c.rate = hz
                self.stats["rate"] = hz
                c.next_snap = 0.0
                self._send(c, w.message(w.T_RATE, bytes([hz])))
            elif typ == w.T_PING and ln >= 4:
                self._send(c, w.message(w.T_PONG, body[:4]))
                self.stats["pongs"] += 1
            elif typ == w.T_ACK and ln >= 4:
                a = w.U32.unpack_from(body)[0]
                self.stats["acks"] += 1
                self.stats.setdefault("first_ack", a)
                # newer than what it holds, never beyond what was sent (lcdmirror_main.c)
                if 0 < ((a - c.acked) & 0xFFFFFFFF) < 0x80000000 and \
                        ((c.seq - a) & 0xFFFFFFFF) < 0x80000000:
                    c.acked = a

    def _encode(self, frame: bytes, tiles: set[int]) -> list[bytes]:
        out = []
        cache = self._enc_cache
        for t in sorted(tiles):
            px = w.tile_of_frame(frame, t)
            hit = cache.get(px)
            if hit is None:
                hit = w.encode_tile(px)
                if len(cache) > 4096:
                    cache.clear()
                cache[px] = hit
            out.append(w.record(t, *hit))
            self.stats["enc_hist"][hit[0]] += 1
        return out

    def _snap(self, c: _Client, now: float) -> None:
        with self.lock:
            p = self.panel
            if self.animate is not None:
                self.animate(p, now)
            frame = bytes(p.gram)
            valid = set(p.valid)
            hdr = (p.frames, p.resets, p.status_word(self.mode), p.owner, bytes(p.regs))
        key = c.key_req
        if key:
            tiles = valid
        else:
            tiles = {t for t in valid if w.tile_of_frame(frame, t) != w.tile_of_frame(c.held, t)}
            tiles |= valid - c.held_valid
            sig = (hdr[1], hdr[2] & ~w.ST_IN_GRAM, hdr[3], w.mode_word(hdr[4]), frozenset(valid))
            if not tiles and sig == c.sig:
                return                                    # nothing changed: no UPDATE
        recs = self._encode(frame, tiles)
        for t in tiles:
            w.put_tile_in_frame(c.held, t, w.tile_of_frame(frame, t))
        c.held_valid = valid
        c.sig = (hdr[1], hdr[2] & ~w.ST_IN_GRAM, hdr[3], w.mode_word(hdr[4]), frozenset(valid))
        parts = w.split_records(recs, w.part_budget(self.max_msg, key))
        c.parts = []
        for i, chunk in enumerate(parts):
            last = i == len(parts) - 1
            flags = w.S_SNAP_LAST if (last and self.snap_last) else 0
            if key:
                flags |= w.S_KEY | (w.S_KEY_FIRST if i == 0 else 0) | (w.S_KEY_LAST if last else 0)
            c.parts.append((chunk, flags))
        c.snap_hdr = (int(now * 1000) & 0xFFFFFFFF, hdr[0], hdr[1], hdr[2], hdr[3],
                      w.valid_bytes(valid), hdr[4])
        self.stats["snaps"] += 1
        self.stats["snap_parts_max"] = max(self.stats["snap_parts_max"], len(parts))
        if key:
            c.key_req = False
            self.stats["keys"] += 1

    def _next_part(self, c: _Client) -> None:
        chunk, flags = c.parts.pop(0)
        t_ms, frames, resets, status, owner, vb, regs = c.snap_hdr
        c.seq = (c.seq + 1) & 0xFFFFFFFF
        m = w.update_msg(c.seq, t_ms, frames, resets, status | flags, owner, vb, chunk, regs=regs)
        with self.lock:
            lose = self._gap > 0 and not flags & w.S_KEY
            if lose:
                self._gap -= 1
        if lose:
            self.stats["gaps"] += 1                       # lost on the way: HM never sees it
            return
        self._send(c, m)
        self.stats["updates"] += 1
        self.stats["tiles"] += len(chunk)
        if flags & w.S_KEY:
            self.stats["key_updates"] += 1
        unacked = (c.seq - c.acked) & 0xFFFFFFFF
        c.max_unacked = max(c.max_unacked, unacked)
        self.stats["max_unacked"] = max(self.stats["max_unacked"], unacked)

    def _maybe_update(self, c: _Client) -> None:
        if not c.room():
            if c.parts or c.key_req:
                self.stats["window_waits"] += 1
            return
        if c.parts:
            self._next_part(c)
            return
        if not c.started or c.rate == 0:
            return                                        # not asking, or paused
        now = time.monotonic()
        if not c.key_req and now < c.next_snap:
            return
        c.next_snap = now + 1.0 / c.rate
        self._snap(c, now)
        if c.parts:
            self._next_part(c)


# --- animations for SNAP-time content ------------------------------------------------------------


class CardAnimator:
    """The clcd_demo card (``lm1_golden.card_picture``): the counter +1 every ``period_s``,
    a full repaint each time (``frames`` +1), as the RM does."""

    def __init__(self, period_s: float = 1 / 12, *, start: int = 0) -> None:
        self.period_s = period_s
        self.counter = start
        self.t0: float | None = None
        self.frozen = False

    def __call__(self, panel: FakePanel, now: float) -> None:
        from tests.fakes.lm1_golden import card_picture

        if self.t0 is None:
            self.t0 = now
            panel.set_frame(card_picture(self.counter), valid=set(range(w.NTILES)))
            return
        if self.frozen:
            return
        want = int((now - self.t0) / self.period_s)
        if want != self.counter:
            self.counter = want & 0xFFFF
            panel.set_frame(card_picture(self.counter), valid=set(range(w.NTILES)))


class NoiseAnimator:
    """The worst case: a fresh random frame every ``period_s`` (incompressible)."""

    def __init__(self, period_s: float = 1 / 12) -> None:
        self.period_s = period_s
        self.next: float | None = None
        self.frozen = False

    def __call__(self, panel: FakePanel, now: float) -> None:
        if self.frozen:
            return
        if self.next is None or now >= self.next:
            self.next = now + self.period_s
            panel.set_frame(os.urandom(w.FRAME_BYTES), valid=set(range(w.NTILES)))


# --- the browser's side, for checks --------------------------------------------------------------


class ViewerModel:
    """What a browser tab holds: each viewer UPDATE (the board's layout, all five
    encodings) applied to a row-major picture. No threads: tests pump it."""

    def __init__(self) -> None:
        self.frame = bytearray(w.FRAME_BYTES)
        self.valid: set[int] = set()
        self.seq: int | None = None
        self.status = 0
        self.owner: int | None = None
        self.messages = 0
        self.keys = 0
        self.tiles = 0

    def apply(self, m: bytes) -> int:
        magic, typ, _r, ln = w.HEADER.unpack_from(m)
        assert magic == w.MAGIC and typ == w.T_UPDATE and ln == len(m) - w.HEADER_SIZE
        u = w.parse_update(m[w.HEADER_SIZE:])
        for rec in u.tiles:
            w.put_tile_in_frame(self.frame, rec.idx, w.decode_tile(rec.enc, rec.payload))
        self.valid = set(w.valid_tiles(u.valid))
        self.seq, self.status, self.owner = u.seq, u.status, u.owner
        self.messages += 1
        self.tiles += len(u.tiles)
        self.keys += bool(u.key)
        return u.seq

    def pump(self, viewer: Any, *, ack: bool = True, limit: int = 10_000) -> int:
        """Pull and apply everything ``viewer`` has now (acking each); how many messages."""
        n = 0
        while n < limit:
            m = viewer.next_message()
            if m is None:
                return n
            seq = self.apply(m)
            if ack:
                viewer.ack(seq)
            n += 1
        return n

    def crc(self, valid: set[int] | None = None) -> int:
        return crc_valid(self.frame, self.valid if valid is None else valid)

    def matches(self, frame: bytes, valid: set[int]) -> bool:
        return self.valid == set(valid) and crc_valid(self.frame, valid) == crc_valid(frame, valid)
