"""The hub SD door: a harness base written to the MPS3 config SD through the lab hub.

Lane HUB-SD (HARNESS-DIST §5 path (b), §6 (b), H10; david's decisions U9 and U10). A
board behind the hub has its Debug USB on the hub, not here, so the config SD is written
by fpgahub (``fpgahub target program TARGET BIT --method sd --force``) and the new base is
started by the paced REBOOT HM already has (``mcc.py``, 100 ms a character over the
share). ``session.hub_sd`` is a ``HubSdDoor``: the ``StorageAdapter`` shape the executor
drives (``backup`` / ``install`` / ``restore`` / ``load_backup`` / ``pending``), so the
install flow, its journal and its confirm stay T7's.

The sequence, single-flight, the lease required (``install``):

1. the delta is ``nanosoc.bit`` only: the new release's SD tree is compared with the
   running release's, file by file (every static so far differs only there; fpgahub's
   ``sd_install`` plugin writes exactly one file, so anything else needs the Debug USB);
2. the backup is verified: the running release's ``nanosoc.bit`` from the signed cache
   (fpgahub cannot back up the SD), zipped in T3's backup format;
3. the lease is this client's (``HeldError`` names the holder), and nobody else is on the
   MCC share ``tty_00`` (one reader: a second one splits the REBOOT);
4. fpgahub offers an ``sd`` program method (FH-d would say whether it can REACH the card);
5. the ``.bit`` is uploaded and its sha checked where it landed;
6. ONE ``program --method sd --force`` request. **Its client times out at 30 s while the
   hub keeps writing (~68 s for 12 MB): that is not a failure, and it is never retried**
   (a retry plus a reset mid-write once left the board dark). ``--force`` always: without
   it the write is silently skipped when that sha is already loaded;
7. completion is proven by the hub's own record naming the sha: the
   ``board.program_completed`` event (REST), the journal line ``program dispatched: …
   ok=True … sha256=<12hex>`` (SSH), or the hub's ``last programmed fingerprint``
   changing to ours. Never the client's exit code. A different sha refuses (no REBOOT);
   no proof within the budget refuses too, and the in-flight marker stays so nothing
   writes again until the hub's record is read.

``restore`` runs the same sequence with the backup's ``.bit`` (a rollback, and U10's
auto-revert of a board that stays dark). The REBOOT itself is the controller's.

Backends. ``SshSdBackend`` (the lab hub today: ``ssh HUB 'sg fpga -c "fpgahub …"'``; the
``.bit`` is staged in the hub user's ``~/.cache/harness-manager/hub-sd``, the daemon reads
it by path as the platform's own runbooks do) and ``RestSdBackend`` (fpgahub's
``/api/v1``: the ``.bit`` goes to the hub's bitstream repository, ``POST /bitstreams``, and
is programmed ``--from`` its id, because a REST client shares no filesystem with the
daemon). Both are thin; every hub fact they parse is cited to fpgahub v0.3.0.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.pack import BackupRecord, Progress

from .sd import BACKUP_FORMAT, MANIFEST_NAME, VOLUME_PREFIX, file_sha256, pid_alive

log = logging.getLogger(__name__)

#: The one file fpgahub's ``sd_install`` plugin writes on the MPS3 (config.toml ``dest``;
#: fpgahub program.py:18-21). HARNESS-DIST §6 (b): the door is allowed only when the SD
#: delta is exactly this file.
NANOSOC_BIT = "MB/HBI0309C/Nanosoc/nanosoc.bit"
VIA_HUB = "hub"
DOOR = "hub_sd"
SD_METHOD = "sd"                      # [boards.mps3_01_pl.program.sd] on the lab hub
MCC_SHARE = "mcc"
#: The hub user's staging directory, relative to its home (ssh runs there).
STAGE_DIR = ".cache/harness-manager/hub-sd"
#: fpgahub's own client window (``ipc.DaemonClient(timeout=30.0)``): the CLI gives up on a
#: 12 MB write after this and prints ``POST /targets/<t>/program: timed out``.
CLIENT_TIMEOUT_S = 30.0
#: Our ssh wrapper's budget: longer than the CLI's window, so the CLI's own timeout is what
#: we read, and a hung ssh is still bounded.
RUN_TIMEOUT_S = 120.0
#: How long to wait for the hub's completion record. The clean write took 68 s (journal,
#: 2026-09-08); "wait 5 min touching nothing" was proven safe (2026-07-18).
COMPLETE_BUDGET_S = 600.0
POLL_S = 5.0
#: A marker whose owner is on another host, or whose write was never proven, is trusted
#: to be live this long (sd.py's STALE_AFTER_S).
STALE_MARKER_S = 1800.0
DESCRIBE_TTL_S = 10.0
EXPECTED_TIMEOUT = "a client timeout here is expected: the hub keeps writing"

# fpgahub program.py:497-502: the daemon's journal line for every finished program.
_JOURNAL_RE = re.compile(
    r"program dispatched: board=(?P<board>\S+) method=(?P<method>\S+) plugin=(?P<plugin>\S+) "
    r"ok=(?P<ok>True|False) part=(?P<part>\S+) sha256=(?P<sha>[0-9a-fA-F]+) "
    r"dur=(?P<dur>[0-9.]+)s")
# uvicorn's access line for a program request the daemon refused or failed (a plugin
# raising ProgramError is HTTP 400: api/v1.py board_program).
_ACCESS_FAIL_RE = re.compile(r'"POST /api/v1/targets/(?P<t>[^/\s]+)/program HTTP/[0-9.]+" '
                             r"(?P<status>[45]\d\d)")
_SHA_WORD = re.compile(r"sha256=([0-9a-fA-F]{8,64})")
_LAST_FP = re.compile(r"last programmed fingerprint:\s*([0-9a-fA-F]{8,64})")
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


# --- results -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgramReply:
    """What the ONE program request answered. ``timeout`` is expected, never a failure."""

    state: str                    # ok | timeout | failed | refused
    message: str = ""
    fingerprint: str = ""         # the sha (or its prefix) the hub named, when it answered


@dataclass(frozen=True)
class Completion:
    """The hub's own record of a finished write (never the client's exit code)."""

    ok: bool
    sha256: str                   # what the hub says it wrote (full, or a 12/16-hex prefix)
    source: str                   # event | journal | hub-state | reply
    detail: str = ""
    dur_s: float | None = None


@dataclass(frozen=True)
class ProgramInfo:
    """``GET /targets/{t}/program`` / ``fpgahub target program T --list``."""

    methods: dict[str, tuple[bool, str]] = field(default_factory=dict)   # name -> (ok, why)
    last_fingerprint: str = ""


def sha_matches(ours: str, theirs: str) -> bool:
    """Does the hub's sha (full or a prefix of at least 8 hex) name our bytes?"""
    a, b = (ours or "").lower(), (theirs or "").lower()
    if len(b) < 8 or len(a) < 8:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


class HubSdBackend(Protocol):
    """What the door needs from fpgahub. ``begin`` opens the completion window (the hub's
    clock, an event subscription) BEFORE the program request; ``end`` closes it."""

    transport: str

    def program_info(self) -> ProgramInfo: ...
    def stage(self, local: Path, sha256: str, progress: Progress) -> str: ...
    def begin(self) -> None: ...
    def program(self, ref: str) -> ProgramReply: ...
    def completion(self, sha256: str) -> Completion | None: ...
    def end(self) -> None: ...


# --- the SSH backend (fpgahub's CLI on the hub) -----------------------------------------------


def parse_program_list(text: str) -> ProgramInfo:
    """``fpgahub target program T --list`` (cli.py:1024-1051): a rich table of Method,
    Plugin, Confirm?, Available?, Description; then ``last programmed fingerprint: <16hex>…``."""
    methods: dict[str, tuple[bool, str]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"[│┃|]", line) if c.strip()]
        if len(cells) < 4 or cells[0].lower() == "method":
            continue
        avail = cells[3].lower()
        if avail.startswith("yes"):
            methods[cells[0]] = (True, "")
        elif avail.startswith("no"):
            why = cells[3][2:].strip(" ()") or "unavailable"
            methods[cells[0]] = (False, why)
    m = _LAST_FP.search(text)
    return ProgramInfo(methods=methods, last_fingerprint=m.group(1).lower() if m else "")


def parse_program_reply(rc: int, text: str) -> ProgramReply:
    """``fpgahub target program … --method sd --force`` as the CLI prints it (cli.py:1071-1086,
    ipc.py:139-143): ``ok T program method=sd plugin=sd_install`` + ``sha256=<16hex>…`` on an
    answer; ``POST /targets/T/program: timed out`` when its 30 s window closed first."""
    flat = " ".join(text.split())
    sha = _SHA_WORD.search(text)
    fp = sha.group(1).lower() if sha else ""
    if rc == 0:
        if re.search(r"(^|\s)ok\s", text):
            return ProgramReply("ok", flat, fp)
        return ProgramReply("failed", flat or "the hub reported a failed program", fp)
    status = _HTTP_STATUS.search(text)
    if status:
        code = int(status.group(1))
        return ProgramReply("refused" if code in (403, 404, 409, 423) else "failed", flat, fp)
    if "timed out" in text.lower() or "timeout" in text.lower():
        return ProgramReply("timeout", flat)
    if "cannot reach fpgahubd" in text:
        return ProgramReply("refused", flat)        # never left the client: nothing written
    return ProgramReply("failed", flat or f"fpgahub exited {rc}")


def parse_journal(text: str, target: str, method: str = SD_METHOD) -> Completion | None:
    """The LAST finished program of ``target`` by ``method`` in ``journalctl -u fpgahubd``
    lines (program.py:497-502), or a refused/failed request (the access log), else None."""
    found: Completion | None = None
    for line in text.splitlines():
        m = _JOURNAL_RE.search(line)
        if m and m.group("board") == target and m.group("method") == method:
            found = Completion(ok=m.group("ok") == "True", sha256=m.group("sha").lower(),
                               source="journal", detail=line.strip(),
                               dur_s=float(m.group("dur")))
            continue
        a = _ACCESS_FAIL_RE.search(line)
        if a and a.group("t") == target:
            found = Completion(ok=False, sha256="", source="journal",
                               detail=f"the hub answered HTTP {a.group('status')}: "
                                      f"{line.strip()}")
    return found


class SshUploader:
    """Put a local file at ``remote_rel`` (relative to the hub user's home) over ssh:
    ``mkdir -p … && cat > X.part && mv -f X.part X``. Never through ``sg``: the file is the
    user's own. ``BatchMode`` so a missing key fails fast."""

    def __init__(self, host: str, *, jump: str = "", timeout_s: float = 600.0) -> None:
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            raise UsageError(f"bad hub host {host!r}")
        if jump and (jump.startswith("-") or any(c.isspace() for c in jump)):
            raise UsageError(f"bad SSH jump host {jump!r}")
        self.host, self.jump, self.timeout_s = host, jump, timeout_s

    def argv(self, remote_rel: str) -> list[str]:
        d = shlex.quote(str(Path(remote_rel).parent))
        part, final = shlex.quote(remote_rel + ".part"), shlex.quote(remote_rel)
        remote = f"mkdir -p {d} && cat > {part} && mv -f {part} {final}"
        opts = ["-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.jump:
            opts += ["-J", self.jump]
        return ["ssh", *opts, self.host, remote]

    def __call__(self, local: Path, remote_rel: str) -> None:
        with open(local, "rb") as fh:
            try:
                proc = subprocess.run(self.argv(remote_rel), stdin=fh, capture_output=True,
                                      timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired as exc:
                raise UnreachableError(f"uploading to the hub {self.host} took over "
                                       f"{self.timeout_s:.0f} s") from exc
            except OSError as exc:
                raise UnavailableError("hub upload", f"cannot run ssh: {exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise UnreachableError(f"uploading to the hub {self.host} failed: {err or proc.returncode}",
                                   hint=f"check `ssh {self.host} true` works")


class SshSdBackend:
    """fpgahub over ssh (``HubClient``'s runner: ``ssh HUB 'sg fpga -c "…"'``)."""

    transport = "ssh"

    def __init__(self, client: Any, *, uploader: Callable[[Path, str], None] | None = None,
                 method: str = SD_METHOD, stage_dir: str = STAGE_DIR,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.target = client.target
        self.method = method
        self.stage_dir = stage_dir
        self._uploader = uploader
        self._clock = clock
        self._since: int | None = None
        self._started = 0.0
        self._fp_before = ""

    @property
    def uploader(self) -> Callable[[Path, str], None]:
        if self._uploader is None:
            self._uploader = SshUploader(self.client.host, jump=getattr(self.client, "jump", ""))
        return self._uploader

    def _run(self, argv: list[str], timeout: float = RUN_TIMEOUT_S) -> Any:
        return self.client._run(argv, timeout=timeout)       # the pack's own recorder

    def program_info(self) -> ProgramInfo:
        try:
            res = self._run(["fpgahub", "target", "program", self.target, "--list"], 60.0)
        except TimeoutError as exc:
            raise UnreachableError(f"the hub {self.client.host} did not list its program "
                                   f"methods in time ({exc})") from exc
        if res.returncode != 0:
            raise UnreachableError(f"fpgahub target program {self.target} --list failed: "
                                   f"{res.text.strip()[:200] or res.returncode}")
        return parse_program_list(res.stdout)

    def stage(self, local: Path, sha256: str, progress: Progress) -> str:
        rel = f"{self.stage_dir}/{sha256}.bit"
        size = Path(local).stat().st_size
        progress("uploading", 0, size)
        self.uploader(Path(local), rel)
        progress("uploading", size, size)
        res = self._run(["sha256sum", rel], 120.0)
        got = res.stdout.split()[0].lower() if res.returncode == 0 and res.stdout.split() else ""
        if got != sha256.lower():
            raise RefusedError(f"the .bit staged on the hub hashes {got[:12] or 'unreadable'}, "
                               f"not {sha256[:12]}: nothing was written",
                               hint="run the install again (the upload is repeated; the SD "
                                    "was not touched)")
        return rel

    def begin(self) -> None:
        self._started = self._clock()
        self._since = None
        with contextlib.suppress(Exception):
            res = self._run(["date", "-u", "+%s"], 30.0)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                self._since = int(res.stdout.strip()) - 5
        with contextlib.suppress(HarnessError):
            self._fp_before = self.program_info().last_fingerprint

    def program(self, ref: str) -> ProgramReply:
        argv = ["fpgahub", "target", "program", self.target, ref, "--method", self.method,
                "--force"]
        try:
            res = self._run(argv, RUN_TIMEOUT_S)
        except TimeoutError as exc:
            # The ssh outlived its budget: the request may be on the hub. Wait, never retry.
            return ProgramReply("timeout", f"the ssh command outlived {RUN_TIMEOUT_S:.0f} s ({exc})")
        return parse_program_reply(res.returncode, res.text)

    def completion(self, sha256: str) -> Completion | None:
        since = (f"@{self._since}" if self._since is not None else
                 f"-{int((self._clock() - self._started) // 60) + 2}min")
        with contextlib.suppress(TimeoutError, HarnessError):
            res = self._run(["journalctl", "-u", "fpgahubd", "--since", since, "--no-pager",
                             "-o", "cat"], 60.0)
            if res.returncode == 0:
                found = parse_journal(res.stdout, self.target, self.method)
                if found is not None:
                    return found
        with contextlib.suppress(HarnessError):
            after = self.program_info().last_fingerprint
            if after and after != self._fp_before and sha_matches(sha256, after):
                return Completion(ok=True, sha256=after, source="hub-state",
                                  detail=f"the hub's last programmed fingerprint is now {after}")
        return None

    def end(self) -> None:
        self._since = None


# --- the REST backend (fpgahub's /api/v1) ------------------------------------------------------


def multipart(fields: Mapping[str, str], file_field: str, filename: str,
              data: bytes) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body (``POST /bitstreams``: api/v1.py bitstreams_upload)."""
    boundary = "hm-" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    out += data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class RestApi:
    """The three things the REST backend asks of fpgahub's API, over ``RestHubClient``'s own
    transport (the Bearer token rides a header only). Tests replace it."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def get(self, path: str) -> Any:
        return self.client._call(f"GET {path}", "GET", path).body

    def post(self, path: str, body: Any, *, timeout: float) -> tuple[str, int, Any]:
        """One POST, never retried: ``("answered", status, body)``, ``("sent", 0, why)`` (it
        may have reached the hub) or ``("not-sent", 0, why)``."""
        from harness_manager.transports import hub_rest

        try:
            resp = self.client._http.request("POST", path, body=body, timeout=timeout)
        except hub_rest._NotSent as exc:
            return "not-sent", 0, str(exc.__cause__ or exc)
        except hub_rest._Sent as exc:
            return "sent", 0, str(exc.__cause__ or exc)
        return "answered", resp.status, resp.body

    def upload(self, path: str, local: Path, fields: Mapping[str, str], *,
               timeout: float = 300.0) -> Any:
        from harness_manager.transports import hub_rest

        http = self.client._http
        body, ctype = multipart(fields, "file", Path(local).name, Path(local).read_bytes())
        conn = http.connection(timeout)
        try:
            conn.request("POST", hub_rest.API_PREFIX + path, body=body,
                         headers=http.headers({"Content-Type": ctype}))
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:
            raise UnreachableError(f"uploading to the hub {self.client.host} failed: {exc}") from exc
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            data = raw.decode("utf-8", "replace")
        if resp.status >= 400:
            raise hub_rest.hub_error(f"POST {path}", resp.status, data, self.client.config)
        return data

    def stream(self, path: str, params: Mapping[str, Any]) -> Iterator[str]:
        conn, resp = self.client._http.open_stream(path, params, timeout=None)
        try:
            for raw in resp:
                yield raw.decode("utf-8", "replace")
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class RestSdBackend:
    """fpgahub over its REST API: upload to the bitstream repository, program ``--from`` it,
    completion from the ``board.program_*`` events (subscribed BEFORE the request: the hub
    keeps no replay buffer) or the target's ``last_fingerprint``."""

    transport = "rest"
    EVENT_TYPES = ("board.program_dispatched", "board.program_completed", "board.program_failed")

    def __init__(self, client: Any, *, api: Any = None, method: str = SD_METHOD,
                 connect_wait_s: float = 5.0) -> None:
        self.client = client
        self.target = client.target
        self.api = api if api is not None else RestApi(client)
        self.method = method
        self.connect_wait_s = connect_wait_s
        self._events: list[dict[str, Any]] = []
        self._mu = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fp_before = ""

    def program_info(self) -> ProgramInfo:
        data = self.api.get(f"/targets/{self.target}/program") or {}
        methods = {m.get("name", ""): (bool(m.get("available")), m.get("unavailable_reason") or "")
                   for m in data.get("methods") or [] if isinstance(m, dict)}
        return ProgramInfo(methods=methods, last_fingerprint=(data.get("last_fingerprint") or "").lower())

    def stage(self, local: Path, sha256: str, progress: Progress) -> str:
        size = Path(local).stat().st_size
        progress("uploading", 0, size)
        out = self.api.upload("/bitstreams", Path(local),
                              {"kind": "bitstream", "name": f"harness-manager {sha256[:12]}",
                               "description": "staged by Harness Manager's hub SD door"})
        progress("uploading", size, size)
        entry = (out or {}).get("bitstream") or {}
        if (entry.get("sha256") or "").lower() != sha256.lower():
            raise RefusedError(f"the hub stored the upload as sha {str(entry.get('sha256'))[:12]}, "
                               f"not {sha256[:12]}: nothing was written",
                               hint="run the install again (the SD was not touched)")
        if not entry.get("id"):
            raise UnreachableError("the hub's upload answer names no bitstream id")
        return str(entry["id"])

    def begin(self) -> None:
        with self._mu:
            self._events = []
        self._stop.clear()
        self._connected.clear()
        with contextlib.suppress(HarnessError):
            self._fp_before = self.program_info().last_fingerprint
        self._thread = threading.Thread(target=self._listen, name="hub-sd-events", daemon=True)
        self._thread.start()
        if not self._connected.wait(self.connect_wait_s):
            log.warning("hub SD door: the event stream did not connect in %.0f s; completion "
                        "falls back to the hub's last_fingerprint", self.connect_wait_s)

    def _listen(self) -> None:
        from harness_manager.transports.hub_events import parse_sse

        def lines() -> Iterator[str]:
            for line in self.api.stream("/events", {"types": ",".join(self.EVENT_TYPES),
                                                    "board": self.target}):
                if line.startswith(":connected"):
                    self._connected.set()
                if self._stop.is_set():
                    return
                yield line

        try:
            for ev in parse_sse(lines()):
                with self._mu:
                    self._events.append(ev)
                if self._stop.is_set():
                    return
        except Exception as exc:  # noqa: BLE001 - a dropped stream falls back to the hub state
            log.info("hub SD door: event stream ended: %s", exc)

    def program(self, ref: str) -> ProgramReply:
        body = {"bitstream_id": ref, "method": self.method, "force": True,
                "skip_if_loaded": False, "confirm": False}
        how, status, data = self.api.post(f"/targets/{self.target}/program", body,
                                          timeout=CLIENT_TIMEOUT_S)
        if how == "not-sent":
            return ProgramReply("refused", f"the request never reached the hub: {data}")
        if how == "sent":
            return ProgramReply("timeout", f"no answer within {CLIENT_TIMEOUT_S:.0f} s ({data})")
        if status == 200 and isinstance(data, dict):
            return ProgramReply("ok" if data.get("ok") else "failed",
                                str(data.get("message") or ""),
                                str(data.get("fingerprint") or "").lower())
        detail = data.get("detail") if isinstance(data, dict) else data
        return ProgramReply("refused" if status in (403, 404, 409, 423) else "failed",
                            f"HTTP {status}: {detail}")

    def completion(self, sha256: str) -> Completion | None:
        with self._mu:
            events = list(self._events)
        done = None
        for ev in events:
            data = ev.get("data") or {}
            if data.get("board") not in (None, self.target) or data.get("method") != self.method:
                continue
            if ev.get("type") in ("board.program_completed", "board.program_failed"):
                done = Completion(ok=ev.get("type") == "board.program_completed",
                                  sha256=str(data.get("fingerprint") or "").lower(),
                                  source="event", detail=str(data.get("message") or ev.get("type")))
        if done is not None:
            return done
        with contextlib.suppress(HarnessError):
            after = self.program_info().last_fingerprint
            if after and after != self._fp_before and sha_matches(sha256, after):
                return Completion(ok=True, sha256=after, source="hub-state",
                                  detail=f"the hub's last_fingerprint is now {after[:16]}")
        return None

    def end(self) -> None:
        self._stop.set()


def backend_for(hub: Any) -> HubSdBackend:
    """The backend for a session's ``Mps3Hub``: REST when its client speaks REST."""
    client = hub.client
    if getattr(client, "transport", "ssh") == "rest":
        return RestSdBackend(client)
    return SshSdBackend(client)


# --- the backup (the previous release's .bit) ---------------------------------------------------


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").strip("/").lower()


def sd_delta(new: Mapping[str, Any], old: Mapping[str, Any]) -> list[str]:
    """The SD paths that differ between two trees (``path -> sha256 or a local file``),
    FAT-style (case-insensitive), sorted. A ``Path`` value is hashed."""
    def shas(tree: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
        out = {}
        for rel, v in tree.items():
            out[_norm(rel)] = (rel, file_sha256(Path(v)) if isinstance(v, Path) else str(v).lower())
        return out

    a, b = shas(new), shas(old)
    return sorted(a[k][0] if k in a else b[k][0]
                  for k in set(a) | set(b) if (a.get(k) or ("", ""))[1] != (b.get(k) or ("", ""))[1])


def write_bit_backup(dest_dir: Path, bit: Path, *, version: str, label: str,
                     now: float) -> BackupRecord:
    """T3's backup format holding one file, ``volume/<NANOSOC_BIT>``, plus a ``.sha256``
    sidecar: what the hub door can restore (fpgahub cannot back up the SD itself)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    sha = file_sha256(bit)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    final = dest_dir / f"hub-sd-{re.sub(r'[^A-Za-z0-9_.-]', '_', version or 'unknown')}-{stamp}.zip"
    n = 1
    while final.exists():
        final = final.with_name(final.stem.rsplit("-", 1)[0] + f"-{stamp}-{n}.zip")
        n += 1
    part = final.with_name(final.name + ".part")
    manifest = {"format": BACKUP_FORMAT, "label": label, "source": f"harness {version} (signed cache)",
                "created_at": now, "files": [{"path": NANOSOC_BIT, "size": bit.stat().st_size,
                                              "sha256": sha, "mtime_ns": 0}],
                "dirs": [], "door": DOOR, "version": version}
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(bit, VOLUME_PREFIX + NANOSOC_BIT)
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1, sort_keys=True))
        os.replace(part, final)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    zsha = file_sha256(final)
    final.with_name(final.name + ".sha256").write_text(f"{zsha}  {final.name}\n", encoding="utf-8")
    return BackupRecord(path=str(final), sha256=zsha, created_at=now, files=1, volume_label=label)


def read_bit_backup(record: BackupRecord, out_dir: Path) -> tuple[Path, str, dict[str, Any]]:
    """Verify a hub-door backup (its sha, its manifest, the one file) and extract the ``.bit``.
    Returns ``(bit, sha256, manifest)``."""
    path = Path(record.path)
    if not path.is_file():
        raise RefusedError(f"backup {path} does not exist", hint="take the install again: it "
                                                               "keeps the previous .bit first")
    if file_sha256(path) != record.sha256:
        raise RefusedError(f"backup {path} fails its sha256 check: it is damaged or not the "
                           "backup that was taken")
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME))
            entries = {e["path"]: e for e in manifest.get("files", [])}
            if manifest.get("format") != BACKUP_FORMAT or set(entries) != {NANOSOC_BIT}:
                raise RefusedError(f"backup {path} is not a hub-door backup (one "
                                   f"{NANOSOC_BIT} in {BACKUP_FORMAT})")
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            bit = out_dir / f"{entries[NANOSOC_BIT]['sha256']}.bit"
            with zf.open(VOLUME_PREFIX + NANOSOC_BIT) as src, open(bit, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise RefusedError(f"backup {path} is unreadable: {exc}") from exc
    sha = file_sha256(bit)
    if sha != entries[NANOSOC_BIT]["sha256"]:
        raise RefusedError(f"backup {path}: {NANOSOC_BIT} does not match its manifest")
    return bit, sha, manifest


# --- single flight --------------------------------------------------------------------------------


_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


class InFlight:
    """One hub SD write per (hub, target), across processes: a marker file in HM's state
    (``update/hub_sd/<host>_<target>.json``) written before the upload and removed only
    when the hub's record proves the write finished (or it never started)."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time,
                 alive: Callable[[int], bool] = pid_alive) -> None:
        self.path = Path(path)
        self.now = now
        self.alive = alive

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"state": "unreadable"}
        return data if isinstance(data, dict) else {"state": "unreadable"}

    def write(self, **fields: Any) -> dict[str, Any]:
        data = self.read() or {"pid": os.getpid(), "host": socket.gethostname(),
                               "started_at": self.now()}
        data.update(fields, updated_at=self.now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        return data

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def owner_alive(self, data: dict[str, Any]) -> bool:
        if data.get("host") != socket.gethostname():
            return self.now() - float(data.get("started_at", 0)) < STALE_MARKER_S
        pid = int(data.get("pid", -1))
        if pid == os.getpid():
            return str(self.path) in _ACTIVE
        return self.alive(pid)


# --- the door -------------------------------------------------------------------------------------


class HubSdDoor:
    """``session.hub_sd`` (see the module doc). A ``StorageAdapter`` whose writes go through
    fpgahub's ``--method sd`` and are proven by the hub's own record of the sha."""

    via = VIA_HUB
    door = DOOR
    only_paths = (NANOSOC_BIT,)
    #: The executor gives this door the running release's SD part (its backup source).
    wants_previous_base = True

    def __init__(self, hub: Any, *, backend: HubSdBackend | None = None, board_id: str = "",
                 mcc_tty: str | None = None, state_dir: Path | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 budget_s: float = COMPLETE_BUDGET_S, poll_s: float = POLL_S,
                 own_linger: Callable[[str], int] | None = None) -> None:
        self.hub = hub
        self.backend = backend
        self.board_id = board_id
        self.mcc_tty = mcc_tty if mcc_tty is not None else _mcc_tty(hub)
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self.clock = clock
        self.sleep = sleep
        self.budget_s = budget_s
        self.poll_s = poll_s
        self._own_linger = own_linger
        self.lease_check: Callable[[str], Any] | None = None
        self.previous: dict[str, Any] | None = None        # {version, files: {rel: Path}}
        self.last: dict[str, Any] = {}                     # the last write's evidence
        self._describe: tuple[float, dict[str, Any]] | None = None

    # -- wiring (the executor) --

    @property
    def target(self) -> str:
        return str(getattr(self.hub, "target", "") or "")

    @property
    def host(self) -> str:
        return str(getattr(self.hub, "host", "") or "")

    def _backend(self) -> HubSdBackend:
        if self.backend is None:
            self.backend = backend_for(self.hub)
        return self.backend

    def bind(self, *, state_dir: Path | None = None,
             lease_check: Callable[[str], Any] | None = None, board_id: str = "") -> None:
        """The executor's context: where the in-flight marker lives, the lease gate."""
        if state_dir is not None:
            self._state_dir = Path(state_dir)
        if lease_check is not None:
            self.lease_check = lease_check
        if board_id:
            self.board_id = board_id

    def set_previous_base(self, version: str, files: Mapping[str, Path]) -> None:
        """The running release's SD tree (verified from the signed cache): the backup's
        ``.bit`` and the tree the delta is measured against."""
        self.previous = {"version": version, "files": dict(files)}

    def _state(self) -> Path:
        if self._state_dir is None:
            from harness_manager.engine import resolve_state_dir

            self._state_dir = Path(resolve_state_dir(None)) / "update"
        return self._state_dir

    def _flight(self) -> InFlight:
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.host}_{self.target}")
        return InFlight(self._state() / "hub_sd" / f"{key}.json", now=self.clock)

    # -- what the planner is told --

    def describe(self) -> dict[str, Any]:
        """``{available, reason, door, hub, target, transport, sd_method, mcc_share,
        only_paths}`` for ``BoardView.hub_sd`` (cached ``DESCRIBE_TTL_S``; never raises)."""
        now = self.clock()
        if self._describe is not None and now - self._describe[0] < DESCRIBE_TTL_S:
            return dict(self._describe[1])
        out: dict[str, Any] = {"available": False, "reason": "", "door": DOOR, "hub": self.host,
                               "target": self.target, "transport": "", "sd_method": False,
                               "mcc_share": bool(self.mcc_tty), "mcc_tty": self.mcc_tty or "",
                               "only_paths": list(self.only_paths)}
        try:
            be = self._backend()
            out["transport"] = be.transport
            info = be.program_info()
        except HarnessError as exc:
            out["reason"] = f"the hub {self.host} did not answer: {exc.message}"
        else:
            ok, why = info.methods.get(SD_METHOD, (False, "no 'sd' program method is configured "
                                                          f"for {self.target} on the hub"))
            out["sd_method"] = ok
            out["last_fingerprint"] = info.last_fingerprint
            if not ok:
                out["reason"] = f"fpgahub cannot write the SD of {self.target}: {why}"
            elif not self.mcc_tty:
                out["reason"] = ("the board's hub table has no MCC share (shares = { mcc = "
                                 f"\"/dev/{self.target}/tty_00\" }}): nothing could REBOOT it")
            else:
                out["available"] = True
        self._describe = (now, out)
        return dict(out)

    # -- StorageAdapter --

    def locate(self) -> str:
        return f"hub://{self.host}/{self.target}:{NANOSOC_BIT}"

    def pending(self) -> dict[str, Any] | None:
        """An unproven hub write (``InFlight``), else None. A write whose owner is gone but
        whose completion the hub records is settled here (the marker is cleared)."""
        flight = self._flight()
        data = flight.read()
        if data is None:
            return None
        if data.get("state") in ("writing", "verifying") and data.get("sha256"):
            with contextlib.suppress(HarnessError):
                be = self._backend()
                done = be.completion(str(data["sha256"]))
                if done is not None and done.ok and sha_matches(str(data["sha256"]), done.sha256):
                    flight.clear()
                    return None
        return {"op": "hub-sd", **data, "backup": data.get("backup") or {}}

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        """The running release's ``nanosoc.bit`` (from the signed cache) as a one-file backup."""
        emit: Progress = progress or (lambda phase, done, total: None)
        prev = self.previous or {}
        files = prev.get("files") or {}
        bit = next((Path(p) for rel, p in files.items() if _norm(rel) == _norm(NANOSOC_BIT)), None)
        if bit is None or not bit.is_file():
            raise RefusedError(
                "the hub door keeps the running release's nanosoc.bit as its backup (fpgahub "
                "cannot back up the SD), and none was prepared",
                hint="the board's running release must be one the channel lists; or install "
                     "with the Debug USB on this machine")
        emit("backup", 0, 1)
        rec = write_bit_backup(dest_dir, bit, version=str(prev.get("version", "")),
                               label=f"hub:{self.target}", now=self.clock())
        emit("backup", 1, 1)
        return rec

    def load_backup(self, path: Path) -> BackupRecord:
        path = Path(path)
        if not path.is_file():
            raise AbsentError(f"backup {path} does not exist", hint="check the path")
        sha = file_sha256(path)
        sidecar = path.with_name(path.name + ".sha256")
        if sidecar.is_file():
            words = sidecar.read_text(encoding="utf-8", errors="replace").split()
            if not words or words[0].lower() != sha:
                raise RefusedError(f"backup {path} does not match its .sha256 sidecar")
        rec = BackupRecord(path=str(path), sha256=sha, created_at=0.0, files=1,
                           volume_label=f"hub:{self.target}")
        with tempfile.TemporaryDirectory(prefix="hubsd-check-") as tmp:
            read_bit_backup(rec, Path(tmp))
        return rec

    def install(self, files: Mapping[str, Path], *, backup: BackupRecord | None,
                progress: Progress | None = None) -> None:
        """Write the release's ``nanosoc.bit`` through the hub (module doc, steps 1-7)."""
        if backup is None:
            raise RefusedError("writing the config SD needs a verified backup of it first")
        new_bit = next((Path(p) for rel, p in files.items() if _norm(rel) == _norm(NANOSOC_BIT)),
                       None)
        if new_bit is None:
            raise RefusedError(f"the release's SD part has no {NANOSOC_BIT}: the hub door "
                               "writes that one file only")
        old = (self.previous or {}).get("files") or {}
        if not old:
            raise RefusedError("the hub door needs the running release's SD tree to prove the "
                               "delta is nanosoc.bit only, and none was prepared")
        delta = [r for r in sd_delta(files, old) if _norm(r) != _norm(NANOSOC_BIT)]
        if delta:
            raise RefusedError(
                f"this release changes more of the config SD than {NANOSOC_BIT} "
                f"({', '.join(delta[:4])}{' …' if len(delta) > 4 else ''}): fpgahub's sd method "
                "writes one file, so the rest would be left behind",
                hint="install it with the MPS3 Debug USB on this machine (nothing was written)")
        with tempfile.TemporaryDirectory(prefix="hubsd-") as tmp:
            read_bit_backup(backup, Path(tmp))            # the backup is readable, now
        self._write(new_bit, why="install", backup=backup, progress=progress)

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        """Write the backup's ``.bit`` back through the same door (rollback; auto-revert)."""
        with tempfile.TemporaryDirectory(prefix="hubsd-") as tmp:
            bit, _sha, _m = read_bit_backup(backup, Path(tmp))
            self._write(bit, why="restore", backup=backup, progress=progress)

    # -- the sequence --

    def preflight(self) -> None:
        """Before anything is downloaded or written: the lease is ours, nobody else is on
        tty_00, and the hub offers an ``sd`` method. The executor calls it up front; every
        write calls it again."""
        if self.lease_check is not None:
            self.lease_check(f"write the config SD of {self.target} through the hub")
        self._check_mcc_share()
        info = self._backend().program_info()
        ok, why = info.methods.get(SD_METHOD, (False, "no 'sd' program method on the hub"))
        if not ok:
            raise UnavailableError("hub SD write", f"fpgahub cannot write the SD of "
                                                   f"{self.target}: {why}")

    def _check_mcc_share(self) -> None:
        """Exactly one reader on tty_00: ours, at REBOOT time. Anyone attached now refuses."""
        if not self.mcc_tty:
            raise UnavailableError("hub SD install", "the board's hub table has no MCC share "
                                                     "(tty_00): nothing could REBOOT it")
        client = self.hub.client
        info = client.share_for(self.mcc_tty)
        if info is None:
            raise AbsentError(f"no fpgahub share for {self.mcc_tty} on {self.host}",
                              hint=f"start it on the hub: fpgahub share start {self.target} "
                                   f"{self.mcc_tty} (the REBOOT goes through it)")
        ours = self._own_linger(self.mcc_tty) if self._own_linger is not None else _own_linger(
            self.hub, self.mcc_tty)
        readers = int(getattr(info, "readers", 0) or 0)
        if readers > ours:
            who = getattr(info, "writer", "") or "another client"
            raise HeldError(
                f"{who} is on the MCC console {self.mcc_tty} ({readers} client(s)): a second "
                "reader on tty_00 splits the REBOOT, so nothing was written",
                holder=who, hint="ask them to close their MCC console, then install again")

    def _write(self, bit: Path, *, why: str, backup: BackupRecord,
               progress: Progress | None) -> None:
        emit: Progress = progress or (lambda phase, done, total: None)
        sha = file_sha256(bit)
        flight = self._flight()
        key = str(flight.path)
        with _ACTIVE_LOCK:
            if key in _ACTIVE:
                raise HeldError(f"a hub SD write to {self.target} is already running in this "
                                "process", hint="wait for it: a slow write is still a write")
            _ACTIVE.add(key)
        try:
            prior = flight.read()
            if prior is not None:
                self._refuse_prior(flight, prior)
            self.preflight()
            be = self._backend()
            flight.write(state="uploading", sha256=sha, why=why, target=self.target,
                         hub=self.host, backup={"path": backup.path, "sha256": backup.sha256})
            try:
                ref = be.stage(bit, sha, emit)
            except BaseException:
                flight.clear()                          # the SD was never touched
                raise
            flight.write(state="writing", ref=ref)
            be.begin()
            try:
                emit("writing", 0, 1)
                reply = be.program(ref)                  # ONE request; never retried
                self.last = {"reply": reply.state, "reply_message": reply.message, "sha256": sha}
                if reply.state in ("refused", "failed"):
                    # The hub answered: it refused (nothing written) or its plugin failed
                    # and finished. Either way nothing is in flight, and nothing reboots.
                    flight.clear()
                    raise self._refusal(reply)
                flight.write(state="verifying", reply=reply.state)
                done = self._await(be, sha, reply, emit)
            finally:
                be.end()
            self.last.update({"completion": done.source, "hub_sha": done.sha256,
                              "detail": done.detail, "dur_s": done.dur_s})
            if not done.ok:
                flight.clear()                         # the hub finished: it failed
                raise ActionFailedError(f"the hub reports the SD write failed: {done.detail}",
                                        hint="nothing to reboot; the board still runs its old "
                                             "image. Check the hub (journalctl -u fpgahubd)")
            if not sha_matches(sha, done.sha256):
                flight.clear()
                raise RefusedError(
                    f"the hub wrote sha256 {done.sha256[:12]}, not ours {sha[:12]} "
                    f"({done.source}): refusing to REBOOT into bytes this install did not send",
                    hint="check who else programs the board; restore with `harness-manager "
                         "update rollback TARGET`")
            emit("verified", 1, 1)
            flight.clear()
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(key)

    def _refusal(self, reply: ProgramReply) -> HarnessError:
        if reply.state == "refused" and ("lease" in reply.message.lower() or "423" in reply.message):
            return HeldError(f"the hub refused the SD write: {reply.message}",
                             hint="take the board's lease first")
        if reply.state == "refused":
            return RefusedError(f"the hub refused the SD write: {reply.message}",
                                hint="nothing was written; the board still runs its old image")
        return ActionFailedError(f"the hub's SD write failed: {reply.message}",
                                 hint="nothing to reboot; the board still runs its old image")

    def _refuse_prior(self, flight: InFlight, prior: dict[str, Any]) -> None:
        if flight.owner_alive(prior) and prior.get("state") != "unreadable":
            raise HeldError(f"a hub SD write to {self.target} is in flight (pid {prior.get('pid')} "
                            f"on {prior.get('host')}): a second write now corrupts the SD",
                            hint="wait for it to finish: a slow write is still a write, never "
                                 "retry it")
        state = prior.get("state", "")
        if state == "uploading":                         # the SD was never touched
            flight.clear()
            return
        sha = str(prior.get("sha256", ""))
        with contextlib.suppress(HarnessError):
            done = self._backend().completion(sha) if sha else None
            if done is not None and done.ok and sha_matches(sha, done.sha256):
                flight.clear()
                return
        raise RefusedError(
            f"an earlier hub SD write to {self.target} (sha {sha[:12] or '?'}, state {state!r}) "
            "was never proven finished",
            hint=f"read the hub's record first (ssh {self.host} journalctl -u fpgahubd | grep "
                 f"'program dispatched'), then remove {flight.path} if it finished; never reset "
                 "the board mid-write")

    def _await(self, be: HubSdBackend, sha: str, reply: ProgramReply,
               emit: Progress) -> Completion:
        """Poll the hub's record until it names a finished write, or the budget runs out."""
        start = self.clock()
        deadline = start + self.budget_s
        while True:
            done = be.completion(sha)
            if done is not None:
                return done
            if reply.state == "ok" and reply.fingerprint:
                # The request was answered in time, and the hub's answer names the sha it
                # wrote (``BoardProgramRun.fingerprint``): the hub's record, not an exit code.
                return Completion(ok=True, sha256=reply.fingerprint, source="reply",
                                  detail=reply.message)
            now = self.clock()
            if now >= deadline:
                break
            emit("verifying", int(now - start), int(self.budget_s))
            self.sleep(self.poll_s)
        raise ActionFailedError(
            f"no record from the hub of the SD write finishing within {self.budget_s:.0f} s "
            f"(the request answered {reply.state!r}): the hub may still be writing",
            hint="do NOT reset the board or retry; read the hub's journal "
                 "(journalctl -u fpgahubd | grep 'program dispatched') and install again once "
                 "it shows ok=True with this sha")


def _mcc_tty(hub: Any) -> str:
    cfg = getattr(hub, "config", None)
    shares = dict(getattr(cfg, "shares", {}) or {})
    if MCC_SHARE in shares:
        return str(shares[MCC_SHARE])
    return next((t for t in shares.values() if str(t).endswith("tty_00")), "")


def _own_linger(hub: Any, tty: str) -> int:
    """Our own just-closed share connections the hub may still count (``hub.SHARES``)."""
    try:
        from .hub import SHARES, ShareRef

        return SHARES.lingering(ShareRef(hub.host, hub.target, tty))
    except Exception:  # noqa: BLE001 - a missing route counts as none of ours
        return 0


# --- the pack hook --------------------------------------------------------------------------------


def make_hub_sd_adapter(session: Any) -> HubSdDoor | None:
    """The ``pack.py`` hook. None when the board has no hub (``session.hub``)."""
    hub = getattr(session, "hub", None)
    if hub is None or getattr(hub, "client", None) is None:
        return None
    cand = getattr(session, "candidate", None)
    return HubSdDoor(hub, board_id=getattr(cand, "board_id", ""))


def describe_for(session: Any) -> dict[str, Any]:
    """``session.hub_sd.describe()``, or ``{}`` for a board with no hub door."""
    door = getattr(session, "hub_sd", None)
    if door is None:
        return {}
    try:
        return door.describe()
    except Exception as exc:  # noqa: BLE001 - a plan never fails for a door's description
        return {"available": False, "reason": f"the hub door could not be read: {exc}",
                "door": DOOR}


__all__ = [
    "COMPLETE_BUDGET_S", "Completion", "DOOR", "EXPECTED_TIMEOUT", "HubSdBackend", "HubSdDoor",
    "InFlight", "NANOSOC_BIT", "ProgramInfo", "ProgramReply", "RestApi", "RestSdBackend",
    "SD_METHOD", "SshSdBackend", "SshUploader", "VIA_HUB", "backend_for", "describe_for",
    "make_hub_sd_adapter", "multipart", "parse_journal", "parse_program_list",
    "parse_program_reply", "read_bit_backup", "sd_delta", "sha_matches", "write_bit_backup",
]
