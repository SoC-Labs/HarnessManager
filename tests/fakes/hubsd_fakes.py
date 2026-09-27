"""HUB-SD fakes: fpgahub's ``--method sd`` (SSH and REST), a board behind it, its MCC share.

``FakeSdHub`` models the lab hub for one target, on an injected clock (``FakeClock``):

- **the SD**: ``sd_bit`` (the bytes at ``MB/HBI0309C/Nanosoc/nanosoc.bit``), written only
  when a program request's write FINISHES, ``write_s`` (68 s, the journal's 2026-09-08
  figure) after the request;
- **the client timeout**: a request whose write outlasts ``client_timeout_s`` (30 s, the
  CLI's own window) answers ``POST /targets/T/program: timed out`` (rc 1) while the write
  goes on; ``writes`` counts every program request, so a test can prove there was ONE;
- **its records**: the daemon's journal line ``program dispatched: board=… method=sd …
  ok=True … sha256=<12hex> dur=…s`` (``journalctl -u fpgahubd --since @EPOCH``), the
  ``board.program_*`` events (REST), and ``last_fingerprint`` (``--list``);
- **faults**: ``write_other`` (the hub writes other bytes: its record names another sha),
  ``fail_write`` (the plugin fails: ``ok=False``), ``refuse`` (HTTP 409 at once),
  ``no_journal`` (journalctl unreadable), ``no_sd_method``;
- **the MCC share**: ``share_readers`` / ``share_writer`` on tty_00 (``fpgahub share list``);
  Harness Manager no longer reads it (MCC-FIX: it never holds a share on tty_00);
- **the MCC ON the hub** (MCC-FIX): pyverify's hub-side writer
  (``python3.11 -c HUB_MCC_REBOOT_PY '<json>'``, found by ``hub_mcc.PY310_PROBE``):
  ``tty_others`` (other readers: rc 3), ``mcc_bare_crlf`` (the next N bare CRs answer only
  ``\r\n``, the post-SD-write quirk: rc 4), the REBOOT itself (``mcc_reboots``,
  ``on_mcc_reboot``) and its captured boot log (``files``, read back with ``cat``); plus
  ``fpgahub whoami --json``, ``date +%s`` and the journal probe ``-n 1`` that pyverify's
  ``sd field --already-written`` asks;
- **the lease**: ``holder`` and ``queue`` (``fpgahub lease show``).

As an SSH runner it is called with the argv ``HubClient`` builds (``__call__``); the
uploader is ``upload``. ``rest_api()`` is the same hub behind ``hub_sd.RestApi``'s three
calls. ``FakeBoard`` is the board: its identity follows the ``.bit`` the MCC last loaded
(T7's fake bitstreams carry the identity), and a bit in ``dark_shas`` never answers.
``HubSession``'s controller is the real ``hub_mcc.HubMccController`` over this runner: a
REBOOT loads ``hub.sd_bit`` into the board (``on_mcc_reboot``), the way the paced REBOOT on
tty_00 reloads the FPGA from the SD. ``FakeMccShare`` (the old share controller) is kept for
tests that want a controller without the hub tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyverify.bootrate import HUB_MCC_REBOOT_PY
from pyverify.lease import RunResult

from harness_manager.core.errors import ActionFailedError, UnreachableError
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager_mps3.hub import HubClient, HubConfig
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_bundles import read_fake_identity

TARGET = "mps3_01_pl"
HUB = "mapstone-dev.ecs.soton.ac.uk"
MCC_TTY = f"/dev/{TARGET}/tty_00"
ME = "dam1n19@mapstone-dev"
PART = "xcku115-flvb2104-2-e"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class _Write:
    ref: str
    data: bytes
    started: float
    done_at: float
    finished: bool = False


class FakeSdHub:
    def __init__(self, clock: FakeClock, *, sd_bit: bytes = b"", target: str = TARGET) -> None:
        self.clock = clock
        self.target = target
        self.sd_bit = sd_bit
        self.write_s = 68.0
        self.client_timeout_s = 30.0
        self.staged: dict[str, bytes] = {}          # hub path / bitstream id -> bytes
        self.writes: list[_Write] = []              # every program request (ONE per install)
        self.journal: list[tuple[float, str]] = []
        self.events: list[dict[str, Any]] = []
        self.last_fingerprint = sha(sd_bit) if sd_bit else ""
        self.calls: list[list[str]] = []
        self.uploads: list[str] = []
        # faults
        self.write_other: bytes | None = None
        self.fail_write = False
        self.refuse = ""
        self.no_journal = False
        self.no_sd_method = False
        self.corrupt_upload = False
        # the MCC share and the lease
        self.share_readers = 0
        self.share_writer = ""
        self.holder = ME
        self.queue: list[str] = []
        self.on_sd_written: list[Any] = []
        # the MCC on tty_00, as pyverify's hub-side writer sees it (MCC-FIX)
        self.hub_python = "/usr/bin/python3.11"
        self.tty_others: list[list[Any]] = []
        self.mcc_bare_crlf = 0
        self.mcc_prompt = "Cmd> "
        self.mcc_reboots = 0
        self.mcc_runs: list[dict[str, Any]] = []
        self.on_mcc_reboot: list[Any] = []
        self.mcc_configures = True
        self.files: dict[str, str] = {}

    # -- the hub's clock ------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        for w in self.writes:
            if not w.finished and now >= w.done_at:
                self._finish(w)

    def _finish(self, w: _Write) -> None:
        w.finished = True
        data = self.write_other if self.write_other is not None else w.data
        fp = sha(data)
        run = f"program-{len(self.writes):012x}"
        ok = not self.fail_write
        self.events.append({"type": "board.program_dispatched", "ts": w.started,
                            "data": {"board": self.target, "method": "sd", "run_id": run,
                                     "fingerprint": fp}})
        if ok:
            self.sd_bit = data
            self.last_fingerprint = fp
            for cb in self.on_sd_written:
                cb(data)
        self.journal.append((w.done_at, "sd_mount: mounting /dev/sdb1 at /run/fpgahub/sd/"
                                         f"{self.target} (vfat)"))
        self.journal.append((w.done_at, f"program dispatched: board={self.target} method=sd "
                                         f"plugin=sd_install ok={ok} part={PART} sha256={fp[:12]} "
                                         f"dur={w.done_at - w.started:.2f}s"))
        self.events.append({"type": "board.program_completed" if ok else "board.program_failed",
                            "ts": w.done_at,
                            "data": {"board": self.target, "method": "sd", "run_id": run,
                                     "fingerprint": fp, "message": f"wrote {len(data)} B"}})

    def _program(self, ref: str) -> tuple[str, dict[str, Any] | str]:
        """``(how, answer)``: ``timeout`` | ``answered`` | ``refused``."""
        self.tick()
        if self.refuse:
            return "refused", self.refuse
        if ref not in self.staged:
            return "refused", f"bitstream not found: {ref}"
        now = self.clock()
        w = _Write(ref, self.staged[ref], now, now + self.write_s)
        self.writes.append(w)
        if self.write_s > self.client_timeout_s:
            return "timeout", f"POST /targets/{self.target}/program: timed out"
        self.clock.advance(self.write_s)
        self.tick()
        data = self.write_other if self.write_other is not None else w.data
        return "answered", {"ok": not self.fail_write, "fingerprint": sha(data),
                            "message": "wrote nanosoc.bit"}

    # -- the SSH side: the runner and the uploader ------------------------------------------

    def upload(self, local: Path, remote_rel: str) -> None:
        data = Path(local).read_bytes()
        self.uploads.append(remote_rel)
        self.staged[remote_rel] = (data[:-1] + b"\0") if self.corrupt_upload else data

    def __call__(self, argv: list[str], timeout: float | None = None) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        self.tick()
        if argv[:3] == ["fpgahub", "target", "program"]:
            if "--list" in argv:
                return RunResult(0, self._list_text(), "")
            ref = argv[4]
            assert argv[5:] == ["--method", "sd", "--force"], argv
            how, answer = self._program(ref)
            if how == "timeout":
                return RunResult(1, "", str(answer) + "\n")
            if how == "refused":
                return RunResult(1, "", f"POST /targets/{self.target}/program → HTTP 409: "
                                        f"{answer}\n")
            assert isinstance(answer, dict)
            tag = "ok" if answer["ok"] else "failed"
            return RunResult(0, f"{tag} {self.target} program method=sd plugin=sd_install\n"
                                f"  bitstream: part={PART} build=- sha256="
                                f"{answer['fingerprint'][:16]}…\n  {answer['message']}\n", "")
        if argv[:2] == ["journalctl", "-u"]:
            if self.no_journal:
                return RunResult(1, "", "Failed to get journal access: Permission denied\n")
            if "--since" in argv:
                since = argv[argv.index("--since") + 1]
                t0 = float(since[1:]) if since.startswith("@") else 0.0
            else:
                t0 = 0.0
            lines = [line for t, line in self.journal if t >= t0]
            if "-n" in argv:
                lines = lines[-int(argv[argv.index("-n") + 1]):]
            return RunResult(0, "".join(f"{line}\n" for line in lines), "")
        if argv[:3] == ["date", "-u", "+%s"] or argv[:2] == ["date", "+%s"]:
            return RunResult(0, f"{int(self.clock())}\n", "")
        if argv[:2] == ["sh", "-c"] and "sys.version_info < (3, 10)" in argv[2]:
            if not self.hub_python:
                return RunResult(127, "", "")
            return RunResult(0, f"{self.hub_python}\n", "")
        if len(argv) == 4 and argv[1:3] == ["-c", HUB_MCC_REBOOT_PY]:
            return self._mcc_writer(argv[0], json.loads(argv[3]))
        if argv[:2] == ["sh", "-c"] and argv[2].startswith("cat --") and len(argv) == 5:
            return RunResult(0, self.files.pop(argv[4], ""), "")
        if argv[:3] == ["fpgahub", "whoami", "--json"]:
            return RunResult(0, json.dumps({"holder": ME, "role": "admin"}) + "\n", "")
        if argv[:1] == ["sha256sum"]:
            data = self.staged.get(argv[1])
            if data is None:
                return RunResult(1, "", f"sha256sum: {argv[1]}: No such file or directory\n")
            return RunResult(0, f"{sha(data)}  {argv[1]}\n", "")
        if argv[:3] == ["fpgahub", "share", "list"]:
            return RunResult(0, self._share_text(), "")
        if argv[:3] == ["fpgahub", "lease", "show"]:
            return RunResult(0, self._lease_text(), "")
        return RunResult(2, "", f"unexpected hub command {argv}\n")

    def _mcc_writer(self, python: str, args: dict[str, Any]) -> RunResult:
        """pyverify's HUB_MCC_REBOOT_PY, as the MCC on tty_00 answers it (module doc)."""
        self.mcc_runs.append({**args, "python": python})
        out: dict[str, Any] = {"tty": args["tty"], "mode": args["mode"], "sent": False,
                               "ack": False, "others": [], "prompt": "", "echo": ""}

        def done(rc: int, reason: str | None = None) -> RunResult:
            out.update(rc=rc, reason=reason)
            return RunResult(rc, json.dumps(out) + "\n", "")

        if args["tty"] != MCC_TTY:
            return done(2, f"no such tty {args['tty']} on this host")
        if self.tty_others:
            out["others"] = [list(o) for o in self.tty_others]
            return done(3, f"another process reads {args['tty']}")
        if args["mode"] == "scan":
            return done(0)
        if self.mcc_bare_crlf > 0:
            self.mcc_bare_crlf -= 1
            out["prompt"] = "\r\n"
            return done(4, "no intact Cmd> after a bare CR")
        out["prompt"] = "\r\n" + self.mcc_prompt
        if "Cmd>" not in self.mcc_prompt:
            return done(4, "Debug> submenu, not Cmd>")
        self.tick()
        out.update(sent=True, ack=True, echo="REBOOT\r\nRebooting...")
        self.mcc_reboots += 1
        for cb in self.on_mcc_reboot:
            cb()
        if float(args.get("capture_s") or 0) > 0:
            from tests.fakes.fake_mcc import BOOT_BANNER

            lines = [ln for ln in BOOT_BANNER if self.mcc_configures
                     or not ln.startswith("FPGA configuration complete")]
            text = "\r\n".join(lines) + "\r\nCmd> "
            if args.get("log"):
                self.files[args["log"]] = text
            out.update(configuring=True, complete=self.mcc_configures, failed=False,
                       log=args.get("log"), tail=text[-400:])
        return done(0)

    def _list_text(self) -> str:
        rows = ["│ default │ vivado_jtag │ no │ yes │ JTAG │"]
        if not self.no_sd_method:
            rows.append("│ sd │ sd_install │ no │ yes │ Write the bitstream to a USB-MSC SD │")
        text = (f"         Program methods ({self.target})\n┃ Method ┃ Plugin ┃ Confirm? ┃ "
                "Available? ┃ Description ┃\n" + "\n".join(rows) + "\n\nexpected_part: xcku115\n")
        if self.last_fingerprint:
            text += f"last programmed fingerprint: {self.last_fingerprint[:16]}…\n"
        return text

    def _share_text(self) -> str:
        return ("┃ TTY ┃ Listen ┃ Writer ┃ Readers ┃ Running ┃\n"
                f"│ {MCC_TTY} │ 0.0.0.0:47001 │ {self.share_writer or '-'} │ "
                f"{self.share_readers} │ yes │\n")

    def _lease_text(self) -> str:
        return f"held by {self.holder} (user dam1n19, expires 2099-01-01T00:00:00+00:00)\n" \
            if self.holder else "not leased\n"

    # -- the REST side ----------------------------------------------------------------------

    def rest_api(self) -> FakeRestApi:
        return FakeRestApi(self)


class FakeRestApi:
    """``hub_sd.RestApi``'s calls against ``FakeSdHub``."""

    def __init__(self, hub: FakeSdHub) -> None:
        self.hub = hub
        self.posts: list[tuple[str, Any]] = []
        self.stopped = False

    def get(self, path: str) -> Any:
        h = self.hub
        h.tick()
        assert path == f"/targets/{h.target}/program", path
        methods = [{"name": "default", "available": True}]
        if not h.no_sd_method:
            methods.append({"name": "sd", "available": True})
        return {"board": h.target, "methods": methods, "last_fingerprint": h.last_fingerprint}

    def post(self, path: str, body: Any, *, timeout: float) -> tuple[str, int, Any]:
        h = self.hub
        self.posts.append((path, dict(body)))
        assert path == f"/targets/{h.target}/program" and body["force"] is True
        assert body["method"] == "sd" and timeout <= 30.0
        how, answer = h._program(body["bitstream_id"])
        if how == "timeout":
            return "sent", 0, "timed out"
        if how == "refused":
            return "answered", 409, {"detail": answer}
        return "answered", 200, answer

    def upload(self, path: str, local: Path, fields: dict[str, str], *,
               timeout: float = 300.0) -> Any:
        h = self.hub
        data = Path(local).read_bytes()
        if h.corrupt_upload:
            data = data[:-1] + b"\0"
        bid = f"bs-{sha(data)[:12]}"
        h.staged[bid] = data
        h.uploads.append(bid)
        return {"bitstream": {"id": bid, "sha256": sha(data), "kind": "bitstream"},
                "created": True}

    def stream(self, path: str, params: dict[str, Any]):
        import time as _time

        yield ":connected\n"
        yield "\n"
        seen = 0
        while not self.stopped:
            self.hub.tick()
            while seen < len(self.hub.events):
                ev = self.hub.events[seen]
                seen += 1
                yield f"event: {ev['type']}\n"
                yield f"data: {json.dumps(ev)}\n"
                yield "\n"
            _time.sleep(0.005)


# --- the board and its MCC share ------------------------------------------------------------------


@dataclass
class FakeBoard:
    """The board's identity follows the bitstream the MCC last loaded."""

    loaded: bytes = b""
    dark_shas: set[str] = field(default_factory=set)
    identity_calls: int = 0

    def identity(self) -> BoardIdentity:
        self.identity_calls += 1
        if not self.loaded or sha(self.loaded) in self.dark_shas:
            raise UnreachableError("no answer from the shell on 6900 (connection refused)")
        ident = read_fake_identity(self.loaded) or {}
        return BoardIdentity(board_type="mps3", shell_id=ident.get("static_id", ""),
                             harness_version=ident.get("harness", ""),
                             firmware_sha=ident.get("sha", ""),
                             features=tuple(ident.get("features") or ()),
                             harness_impl="bare-metal")


class FakeMccShare:
    """The controller over the hub's MCC share: a paced REBOOT reloads the FPGA from the SD."""

    def __init__(self, hub: FakeSdHub, board: FakeBoard) -> None:
        self.hub = hub
        self.board = board
        self.reboots = 0
        self.last_reboot = None

    def reboot(self, progress=None, wait_s: float | None = None) -> dict:
        self.reboots += 1
        self.hub.tick()
        self.board.loaded = self.hub.sd_bit
        if progress:
            progress("sent", 1, 3)
            progress("down", 2, 3)
        if sha(self.board.loaded) in self.board.dark_shas:
            raise ActionFailedError(f"the board went down after REBOOT (the shell stopped "
                                    f"answering ping) but did not come back within "
                                    f"{wait_s or 120:.0f}s: the shell never answered ping again")
        if progress:
            progress("up", 3, 3)
        return {"summary": "REBOOT witnessed over the hub share", "pace_s": 0.1}


class FakeHubHandle:
    """What ``session.hub`` is (``Mps3Hub``): the config, the host/target, the client."""

    def __init__(self, client: Any, *, shares: dict[str, str] | None = None) -> None:
        self.config = HubConfig(host=HUB, target=TARGET,
                                shares=dict(shares if shares is not None else {"mcc": MCC_TTY}))
        self.host = HUB
        self.target = TARGET
        self.client = client
        self.transport = getattr(client, "transport", "ssh")


class HubSession:
    """A board session behind the hub: no Debug USB here (``storage`` None), a hub, the
    hub door, and the MCC ON the hub (``hub_mcc.HubMccController`` over the fake runner) as
    the controller: a REBOOT loads the hub's SD into the board."""

    def __init__(self, hub: FakeSdHub, board: FakeBoard, door: Any, handle: FakeHubHandle, *,
                 board_id: str = "mps3@192.168.10.101") -> None:
        from harness_manager_mps3.hub_mcc import HubMccController

        self.candidate = Candidate(pack="mps3", board_id=board_id,
                                   links=(Link(LinkKind.ETHERNET, "192.168.10.101:6900"),))
        self.board = board
        self.hub = handle
        self.hub_sd = door
        self.storage = None
        hub.on_mcc_reboot.append(lambda: setattr(board, "loaded", hub.sd_bit))
        self.controller = HubMccController(handle.client._run, target=TARGET, tty=MCC_TTY,
                                           host=HUB, session=self, clock=hub.clock,
                                           sleep=hub.clock.sleep)

    def identity(self) -> BoardIdentity:
        return self.board.identity()


def ssh_client(hub: FakeSdHub) -> HubClient:
    return HubClient(HUB, TARGET, runner=hub)


class FakeLeases:
    """``LeaseService.view`` / ``forget`` over ``FakeSdHub.holder`` and its queue."""

    def __init__(self, hub: FakeSdHub) -> None:
        self.hub = hub

    def forget(self, _hub: Any) -> None:
        return None

    def view(self, _hub: Any, *, cached_only: bool = False, max_age_s: float | None = None):
        h = self.hub
        lease = {"holder": h.holder, "mine": h.holder == ME} if h.holder else None
        return {"lease": lease, "queue": [{"position": i + 1, "holder": q, "mine": False}
                                          for i, q in enumerate(h.queue)]}
