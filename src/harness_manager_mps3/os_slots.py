"""The MPS3's OS-slot client for the Linux harness (HARNESS-DIST H12, lane LINUX-SLOTS).

``make_os_slot_adapter(session)`` is the ``pack.py`` hook (``BoardSession.os_slots``,
CCR T7-2). It speaks the v0.14 ``slot`` verb and the 6910 kind-2 push through
``pyverify.slot`` (the one codec), and turns the board's answers into
``core.pack.SlotStatus`` and exit-coded ``HarnessError``s.

The flow (net-protocol.md "Slot images"; SLOT_VERB_DRAFT.md §1)::

    status -> push (kind 2, static_id = the image's PROVISIONED static) -> poll the job
           -> commit -> reboot (witnessed) -> status: running == default == the new slot,
              and target == the OTHER slot (where the next push goes)

**Which harness.** Only ``version.impl == "linux"`` has the slot verbs, and no harness
reports a ``features`` entry for them yet (the Linux lead appends ``slot``, S3; HM matches
the NAME, never a bit number). So: a bare-metal harness is refused WITHOUT sending
anything; a Linux harness that answers ``unknown op`` / ``slot not supported`` is an
older image. A feature named ``slot`` is honoured when a harness reports it.

**Two locks** (``slot_words``): the claim lock below, and the fabric IDENTITY lock
(``identity lock: <reason>``: the card's image and the FPGA's static disagree, claimed or
not), which is fixed by pushing the right image, committing it and rebooting. A failed or
refused act is classified by the reply's ``code`` when the harness sends one (additive,
S3), else by its ``err`` text.

**The lock** (David, 2026-09-24). Once the board's SSH is claimed, ``commit``,
``rollback`` and the push are accepted only from the board itself. ``status`` and
``verify`` stay open. The adapter:

- asks the session's claim first (``claim.lock_route``, lane CLAIMED-LOCK): a board this
  Harness Manager claimed or adopted gets ``commit``/``rollback`` and the push through the
  board's own SSH at once; a board claimed by another key, or with no pin here, is
  ``ClaimLockedError`` (the claim hint) before anything is sent;
- otherwise sends ``commit``/``rollback`` direct; on ``slot locked: board claimed (use ssh)``
  it asks the claim again, now knowing the board is claimed, and sends the SAME request
  through the board's SSH when the claim is ours (a locked refusal changes nothing, so the
  retry is safe), else raises ``ClaimLockedError`` with the claim hint;
- decides the push route BEFORE pushing, because a locked push is closed unread and
  says nothing: by ``identify.ssh.claimed`` (UDP 6899) when identify answers; when it
  cannot (UDP does not cross the hub's SSH tunnel), by a LOCK PROBE: ``rollback``
  guarded with the current default slot. A rollback always picks the slot that is not
  the default, so an unlocked board refuses the guard (``slot mismatch``) and changes
  nothing, while a locked one answers the lock refusal. When ``slot status`` carries
  ``claimed`` (the Linux lead's S2, additive) that answers, and neither identify nor the
  probe is asked.

The board-SSH forward is the session's ONE claim forward (``claim.hold_forward``: ``ssh -J
HUB -l root <pinned host key, claimed key> -L ...:127.0.0.1:6900 -L ...:6910 ... BOARD``),
shared with debug, XVC and the card, held for the length of one mutation.

**Rule 1.** After a ``commit`` and before the reboot the board has no push target.
``push`` refuses that locally with the fix (``slot rollback``); the update executor
rolls back itself before a second push (HARNESS_DISTRIBUTION §9 item 8).

**What this host pushed.** The board knows an image by its S0LB table CRC
(``hdr_crc``), not by a sha256 or a release. The adapter keeps, per board, which
image (sha256, release version) it pushed under which ``hdr_crc``
(``<state>/slots/<board>.json``), so ``status`` can name what each slot holds.

**Timing (lane SLOT-TIMING).** Silicon B2 (2026-09-26): the user microSD wrote at ~70 KB/s
and read back at 14-135 KB/s, so a 29 MB slot took ~12 min to write and ~35 min to verify;
the usd_spi fix after that run lowers the SPI clock, so the card gets slower still. The
card's rates come from ONE place, the rows ``mps3.slot.card_write_bps`` and
``mps3.slot.card_read_bps`` (``card_rates``), and every budget is derived from them. THE
guard is a stall: a job whose byte count has not moved for ``mps3.slot.push_timeout_s`` (900 s) is
stuck, and so is a push whose 64 KiB chunk has not gone for as long (pyverify's
``push_slot_image(timeout_s=)``, never a bound on the transfer). The backstop is a cap on the
whole job, from the first byte sent: ``max(mps3.slot.job_timeout_s, size / rate each way
x1.5)`` (1800 s floor; ~62 min for 29 MB at the defaults); a verify's is the read-back alone,
and a read-back the board does not count bytes for has the cap only. While a job runs,
``status()`` carries this process's estimates (``job.rate_bps``, ``job.eta_s``: from the
rate it observes once the bytes move, else the card's rates), and ``push``/``verify`` report
them (``core.pack.report_progress``). A stuck job, a failed read-back, or a card that stops
answering is an error that says the card's state; nothing is ever written twice, and
nothing here starts a verify (``status`` only). pyverify's ``wait_job`` (180 s default) is
never called: this adapter waits itself, and every pyverify call here passes its budget by
name (never a pyverify default: tests/unit/test_pyverify_budgets.py). ``reboot()`` refuses
while the card job is writing or verifying (``services.reset_guard``; B2: a reboot mid-job
left the card in "uSD init error"). harnessd from platform 53f49b4 on also refuses its own
``reboot`` then (``EBUSY``, as it does mid-swap); older images do not. A job that started
after the guard read the card (another host's push) is refused in the guard's words
(``card_job_refusal``), never as "another client holds the control port".

Test seams: ``poll_s``/``poll_max_s``/``job_timeout_s``/``push_stall_s``/``reboot_poll_s``
on the adapter; the push port follows the deploy adapter's
(``HARNESS_MANAGER_MPS3_PUSH_PORT``); the board-SSH forward uses
``tunnel.DEFAULT_LAUNCHER``/``DEFAULT_SSH_G``; identify uses
``HARNESS_MANAGER_MPS3_IDENTIFY_PORT``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pyverify import slot as pv_slot
from pyverify.pusher import PushError

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.pack import Progress, SlotInfo, SlotJob, SlotStatus, report_progress

from . import ctlgate, slot_words
from .constants import IMPL_LINUX, PUSH_PORT

log = logging.getLogger(__name__)

CAPABILITY = "OS slot update"
#: The feature name HM asked the Linux lane for (HARNESS_DISTRIBUTION §9 item 5).
SLOT_FEATURE = "slot"
SLOT_LOCKED_ERR = pv_slot.SLOT_LOCKED_ERR
#: How the two engines decline a verb they do not have.
_NO_VERB = ("unknown op", "slot not supported")
#: Poll the card job every 0.5 s at first (HARNESS_DISTRIBUTION §9 item 3), then every
#: ``POLL_MAX_S``: each ``slot status`` reads ~7 sectors off the same card the job is using.
POLL_S = 0.5
POLL_MAX_S = 2.0
#: A reboot witness polls this often, and gives up after the caller's budget.
REBOOT_POLL_S = 0.5

# --- the card's timing (SLOT-TIMING) ------------------------------------------------------------
#: The card's rates, bytes a second, from ONE place: these rows (settings.py), whose defaults
#: are the Linux lead's B2 measurements (2026-09-26, the real board, the first usd_spi run):
#: written at ~70 KB/s; read back at 14-135 KB/s, varying a lot, so the default is the
#: slowest seen (~73 s/MB). A 29 MB slot took ~12 min to write and ~35 min to verify. That
#: link was marginal and its fix lowers the SPI clock, so the card will get SLOWER: put the
#: measured rates in these rows when there are some. Every budget and every first ETA uses
#: them; once the job's bytes move, the ETA uses the rate it observes.
CARD_WRITE_BPS_KEY = "mps3.slot.card_write_bps"
CARD_WRITE_BPS_ENV = "HARNESS_MANAGER_MPS3_SLOT_CARD_WRITE_BPS"
CARD_WRITE_BPS = 70_000
CARD_READ_BPS_KEY = "mps3.slot.card_read_bps"
CARD_READ_BPS_ENV = "HARNESS_MANAGER_MPS3_SLOT_CARD_READ_BPS"
CARD_READ_BPS = 14_000
#: The cap is the card's time for the job (size / rate, each direction) times this.
BUDGET_MARGIN = 1.5
#: THE guard is the stall: a job whose byte count has not moved for ``mps3.slot.push_timeout_s``
#: (900 s; pyverify's per-chunk push limit is the same number) is stuck. The whole-job cap,
#: ``max(mps3.slot.job_timeout_s, the size's)``, 1800 s at least, is the backstop (and the
#: only bound on a read-back the board does not count). pyverify's ``slot push`` CLI defaults
#: (180 s, 30 s) were too short on silicon; the Linux lead ran B2 with ``--timeout 1800
#: --push-timeout 900``, and platform 6e6a2a9 raised them to 3600 s / 600 s. HM never takes a
#: pyverify default: these rows are passed by name (tests/unit/test_pyverify_budgets.py).
JOB_TIMEOUT_KEY = "mps3.slot.job_timeout_s"
JOB_TIMEOUT_ENV = "HARNESS_MANAGER_MPS3_SLOT_JOB_TIMEOUT_S"
JOB_TIMEOUT_S = 1800.0
STALL_KEY = "mps3.slot.push_timeout_s"
STALL_ENV = "HARNESS_MANAGER_MPS3_SLOT_PUSH_TIMEOUT_S"
STALL_S = 900.0
#: While a job runs, ``slot status`` may fail now and then (the control port is one client
#: at a time): a wait keeps reading for this long before it gives up with the last state.
STATUS_GRACE_S = 60.0
#: Host-side records kept per board (newest first).
RECORDS_KEEP = 16

RULE1_HINT = ("the board has a commit that has not been booted yet, so no slot is free "
              "(rule 1): roll it back first with `harness-manager slot rollback TARGET`, "
              "or reboot into it")


def _hex32(value: Any) -> str:
    """A u32 as ``0x`` + 8 lowercase hex digits; "" for none."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        n = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return str(value).lower()
    return f"0x{n & 0xFFFFFFFF:08x}"


def same_u32(a: Any, b: Any) -> bool:
    x, y = _hex32(a), _hex32(b)
    return bool(x) and x == y


# --- timing: the budget, and the estimate -------------------------------------------------------


def _setting_s(key: str) -> float:
    """A number row's value (seconds, bytes a second) where it is read: the pack's settings
    reader (``settings.value``, lane SET-WIRE): the row's variable, then the Settings menu /
    ``settings.toml``, then the admin's ``[default]``, then the row's default (the constants
    above). Each row's check takes only a number above 0: a bad variable is refused, naming
    it (``UsageError``); a bad file value is skipped, logged once, for the next layer."""
    from .settings import value

    return float(value(key))


def card_rates() -> tuple[float, float]:
    """``(write, read)`` bytes a second: the card's rates, from their rows."""
    return (_setting_s(CARD_WRITE_BPS_KEY),       # CARD_WRITE_BPS_ENV first
            _setting_s(CARD_READ_BPS_KEY))        # CARD_READ_BPS_ENV first


@dataclass(frozen=True)
class SlotTimeouts:
    """The budget of one card job (module docstring, "Timing")."""

    job_s: float            # the cap on the whole job, from the first byte sent
    push_stall_s: float     # THE guard: no bytes moved for this long (push chunk, job's got)
    setting_s: float        # mps3.slot.job_timeout_s (the cap's floor)
    size_s: float           # what the image's size asks for at the card's rates
    write_bps: float = 0.0
    read_bps: float = 0.0


def budget_s(nbytes: int, *, write: bool = True) -> float:
    """What a card job of ``nbytes`` may take: written (unless ``write`` is False) and read
    back at the card's rates (``card_rates``), x ``BUDGET_MARGIN``."""
    wbps, rbps = card_rates()
    t = nbytes / rbps + (nbytes / wbps if write else 0.0)
    return BUDGET_MARGIN * t


def slot_timeouts(nbytes: int, *, write: bool = True) -> SlotTimeouts:
    """The budget of a push (``write``) or a verify of ``nbytes``, from the card's rates: the
    stall ``max(mps3.slot.push_timeout_s, 64 KiB / write rate x1.5)`` (THE guard), and the cap on
    the whole job ``max(mps3.slot.job_timeout_s, size / rate each way x1.5)`` (a 29 MB push
    at the default rates: ~62 min; 1800 s alone would cut its read-back)."""
    from pyverify.pusher import TCP_SEND_CHUNK

    wbps, rbps = card_rates()
    floor = _setting_s(JOB_TIMEOUT_KEY)           # JOB_TIMEOUT_ENV first
    stall = _setting_s(STALL_KEY)                 # STALL_ENV first
    size = budget_s(nbytes, write=write)
    return SlotTimeouts(job_s=max(floor, size),
                        push_stall_s=max(stall, BUDGET_MARGIN * TCP_SEND_CHUNK / wbps),
                        setting_s=floor, size_s=size, write_bps=wbps, read_bps=rbps)


class JobMeter:
    """Rate and ETA of a board's card job, from the ``status`` samples this process read.

    A phase (writing, verifying) whose ``got`` moved over at least a second has a measured
    rate; otherwise the card's rates stand in (``card_rates``: an ETA that errs long). The ETA is
    to the END of the job: a write's includes its read-back. A read-back the board does not
    count (``got`` stays put) is timed from when this process first saw it verifying."""

    MIN_ETA_S = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._mu = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def forget(self, board_id: str) -> None:
        with self._mu:
            self._runs.pop(board_id, None)

    def estimate(self, board_id: str, job: SlotJob) -> SlotJob:
        """``job`` with ``rate_bps`` and ``eta_s`` (unchanged when it is not busy)."""
        if not job.busy:
            self.forget(board_id)
            return job
        now = self._clock()
        key = (job.act, job.slot, job.length)
        with self._mu:
            run = self._runs.get(board_id)
            if run is None or run["key"] != key:
                run = self._runs[board_id] = {"key": key, "phase": "", "t0": now,
                                              "got0": job.got}
            if run["phase"] != job.state:
                run.update(phase=job.state, t0=now, got0=job.got)
            t0, got0 = run["t0"], run["got0"]
        moved, took = job.got - got0, now - t0
        measured = moved > 0 and took >= 1.0
        left = max(job.length - job.got, 0)
        wbps, rbps = card_rates()
        if job.state == "writing":
            rate = moved / took if measured else wbps
            eta = left / rate + job.length / rbps
        elif measured:                                   # the board counts its read-back
            rate = moved / took
            eta = left / rate
        else:
            rate = rbps
            eta = job.length / rbps - took
        return replace(job, rate_bps=rate, eta_s=max(eta, self.MIN_ETA_S))


#: One meter per process: the daemon's repeated reads of a board sharpen the same estimate.
METER = JobMeter()


# --- the board's status -----------------------------------------------------------------------


def parse_status(raw: dict[str, Any]) -> SlotStatus:
    """``slot`` reply (every act answers the same object) -> ``SlotStatus``."""
    slots: dict[str, SlotInfo] = {}
    for name in ("A", "B"):
        one = raw.get(name.lower())
        if not isinstance(one, dict):
            continue
        slots[name] = SlotInfo(
            name=name, state=str(one.get("state") or ""), hdr_crc=_hex32(one.get("hdr_crc")),
            length=int(one.get("len") or 0), sid=_hex32(one.get("sid")),
            verified=str(one.get("verified") or "no"), err=str(one.get("err") or ""))
    job = raw.get("job") if isinstance(raw.get("job"), dict) else {}
    return SlotStatus(
        running=str(raw.get("running") or "unknown"), slots=slots,
        card=bool(raw.get("card", False)), default=str(raw.get("default") or ""),
        target=str(raw.get("target") or ""), staged=str(raw.get("staged") or ""),
        fabric_sid=_hex32(raw.get("fabric_sid")), seq=int(raw.get("seq") or 0),
        job=SlotJob(act=str(job.get("act") or "none"), slot=str(job.get("slot") or ""),
                    state=str(job.get("state") or "idle"), got=int(job.get("got") or 0),
                    length=int(job.get("len") or 0), err=str(job.get("err") or "")),
        raw=dict(raw))


def status_json(st: SlotStatus) -> dict[str, Any]:
    """What the CLI and the API show of a ``SlotStatus`` (``services.slots``)."""
    from harness_manager.services.slots import slot_status_json

    return slot_status_json(st)


def s0lb_regions(image: bytes) -> list[dict[str, Any]]:
    """The S0LB entry table: ``[{src_offset, dst, len, crc32}]`` (after ``image_info``)."""
    import struct

    n = struct.unpack_from("<I", image, 8)[0]
    out = []
    for i in range(n):
        src, dst, ln, crc = struct.unpack_from("<4I", image, 32 + 16 * i)
        out.append({"src_offset": src, "dst": dst, "len": ln, "crc32": crc})
    return out


def check_image(image: bytes) -> dict[str, Any]:
    """stage0's rules on a boot image, host side, before a byte leaves: an S0LB v2 table
    whose CRC holds (``pyverify.slot.image_info``) and whose every region lies inside the
    image and carries its CRC. Returns ``{hdr_crc, entries, entry_pc, regions}``; raises
    ``RefusedError`` naming the fault. The board runs the same check and is the authority."""
    try:
        info = pv_slot.image_info(image)
    except pv_slot.SlotError as exc:
        raise RefusedError(f"not a boot image stage0 would take: {exc}",
                           hint="push the release's linux_slot.img (stage0_pack.py S0LB v2)") \
            from exc
    regions = s0lb_regions(image)
    for i, r in enumerate(regions):
        end = r["src_offset"] + r["len"]
        if end > len(image):
            raise RefusedError(f"boot image region {i} runs past the end of the file "
                               f"({end} > {len(image)} B): truncated")
        if zlib.crc32(image[r["src_offset"]:end]) & 0xFFFFFFFF != r["crc32"]:
            raise RefusedError(f"boot image region {i} fails its CRC: the file is corrupt",
                               hint="download it again; nothing was pushed")
    return {"hdr_crc": _hex32(info["hdr_crc"]), "entries": info["entries"],
            "entry_pc": _hex32(info["entry_pc"]), "regions": regions}


# --- what this host pushed ---------------------------------------------------------------------


class SlotRecords:
    """Per board: which image (sha256, release version) this host pushed under which
    ``hdr_crc``. The board's status carries only the ``hdr_crc``."""

    def __init__(self, board_id: str, directory: Path | None = None) -> None:
        if directory is None:
            from harness_manager.engine import resolve_state_dir

            directory = resolve_state_dir() / "slots"
        self.path = directory / (re.sub(r"[^A-Za-z0-9_.-]+", "_", board_id) + ".json")

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("pushed") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    def get(self, hdr_crc: str) -> dict[str, Any]:
        want = _hex32(hdr_crc)
        return next((r for r in self._read() if r.get("hdr_crc") == want), {}) if want else {}

    def put(self, hdr_crc: str, *, sha256: str, version: str) -> None:
        want = _hex32(hdr_crc)
        rows = [r for r in self._read() if r.get("hdr_crc") != want]
        rows.insert(0, {"hdr_crc": want, "sha256": sha256.lower(), "version": version,
                        "at": time.time()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pushed": rows[:RECORDS_KEEP]}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# --- the board's own SSH (the lock's way through) -----------------------------------------------


@contextlib.contextmanager
def claim_forward(session: Any, user: str, what: str) -> Iterator[tuple[str, int, int]]:
    """``(host, control, push)`` through the session's claim forward (the board's own SSH to
    its 127.0.0.1), held for the ``with``. A session with no claim adapter cannot have one."""
    claim = getattr(session, "claim", None)
    if claim is None or not callable(getattr(claim, "hold_forward", None)):
        raise UnreachableError(f"{what}: the board is claimed and this session has no SSH to it",
                               hint="`harness-manager board claim-status TARGET`")
    try:
        t = claim.hold_forward(user)
    except UnreachableError as exc:            # only the forward's own failure is worded here
        raise UnreachableError(
            f"the board is claimed, so {what} must go through its SSH, and that did not come up: "
            f"{exc.message}", hint=exc.hint or "check `harness-manager board claim-status "
                                               "TARGET` (your key is the claimed one)") from exc
    try:
        yield "127.0.0.1", t.local_port("control"), t.local_port("push")
    finally:
        claim.release_forward(user)


# --- the adapter --------------------------------------------------------------------------------


class Mps3OsSlots:
    """``core.pack.OsSlotAdapter`` for an MPS3 session (module docstring)."""

    def __init__(self, session: Any, *, poll_s: float = POLL_S, poll_max_s: float = POLL_MAX_S,
                 job_timeout_s: float | None = None, push_stall_s: float | None = None,
                 reboot_poll_s: float = REBOOT_POLL_S, records: SlotRecords | None = None,
                 meter: JobMeter | None = None) -> None:
        self._session = session
        self.poll_s = poll_s
        self.poll_max_s = poll_max_s
        #: None: from the settings and the image's size (``slot_timeouts``); a number: fixed.
        self.job_timeout_s = job_timeout_s
        self.push_stall_s = push_stall_s
        self.reboot_poll_s = reboot_poll_s
        self.meter = meter or METER
        #: The budget the last push/verify ran under (tests, and the hand-back's evidence).
        self.last_timeouts: SlotTimeouts | None = None
        self._records = records
        #: How the last mutation reached the board: "direct" or "board-ssh".
        self.last_route = ""
        #: The last reboot's witness (the executor reads ``down_after_s`` etc.).
        self.last_reboot: Any = None

    # -- plumbing -----------------------------------------------------------------------------

    @property
    def records(self) -> SlotRecords:
        if self._records is None:
            self._records = SlotRecords(self._session.candidate.board_id)
        return self._records

    def _shell(self) -> Any:
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnavailableError(CAPABILITY, "no Ethernet link to the harness")
        return shell

    def _push_port(self) -> int:
        deploy = getattr(self._session, "deploy", None)
        port = getattr(deploy, "push_port", None)
        if isinstance(port, int) and port > 0:
            return port
        return getattr(self._session, "push_port", None) or PUSH_PORT

    def _one(self, host: str, port: int, act: str, slot: str | None) -> dict[str, Any]:
        """One ``slot`` request; the reply (ok or not) as a dict. Transport failures are
        ``HarnessError``s; a refusal is returned for the caller to judge."""
        if slot not in (None, "A", "B"):
            raise UsageError(f"slot must be A or B, not {slot!r}")
        # SERIAL-6900: pyverify opens this connection itself; it still holds the board's
        # control gate (direct or through the claim forward: the same single-client port).
        shell = getattr(self._session, "shell", None)
        forward = shell is not None and (host, port) != (shell.host, shell.port)
        with ctlgate.held(shell, f"slot {act}", lagging=True if forward else None) as gate:
            reply = self._one_request(host, port, act, slot)
            if gate is not None:
                gate.note_close()
            return reply

    @staticmethod
    def _one_request(host: str, port: int, act: str, slot: str | None) -> dict[str, Any]:
        try:
            return pv_slot.slot_request(host, act, slot, port=port, timeout=5.0)
        except pv_slot.SlotError as exc:
            return dict(exc.reply or {"ok": False, "err": str(exc)})
        except ConnectionRefusedError as exc:
            raise UnreachableError(f"the harness at {host}:{port} refused the connection",
                                   hint="is the board up and mps3-harnessd running?") from exc
        except ConnectionError as exc:
            raise HeldError(f"the harness at {host}:{port} kept turning the connection away",
                            hint="another client holds the control port (one client at a "
                                 "time); try again") from exc
        except (OSError, ValueError) as exc:
            raise UnreachableError(f"cannot ask the harness at {host}:{port}: {exc}") from exc

    def _ask(self, act: str, slot: str | None = None) -> dict[str, Any]:
        shell = self._shell()
        return self._one(shell.host, shell.port, act, slot)

    # -- facts --------------------------------------------------------------------------------

    def _live(self) -> Any:
        return self._shell().live()

    def slots_reason(self) -> str:
        try:
            live = self._live()
        except HarnessError as exc:
            if type(exc).__name__ == "ShellRescueError":
                return ("the board is in stage0 RESCUE (no bootable slot): provisioning a slot "
                        "from rescue is not built yet")
            return f"the harness did not answer: {exc.message}"
        if not live.version_ok:
            return "the harness does not answer 'version': it predates the slot verbs"
        if live.impl != IMPL_LINUX and SLOT_FEATURE not in live.features:
            return (f"the {live.impl or 'bare-metal'} harness has no OS slots (the slot verbs "
                    "are the Linux harness's, net-protocol v0.14)")
        try:
            raw = self._ask("status")
        except HarnessError as exc:
            return f"slot status failed: {exc.message}"
        if not raw.get("ok"):
            err = str(raw.get("err", ""))
            if any(k in err.lower() for k in _NO_VERB) or \
                    slot_words.reply_code(raw) == "not_supported":
                return f"this Linux harness does not have the slot verb ({err}): update its image"
            return f"slot status refused: {err}"
        st = parse_status(raw)
        if not st.card:
            return "no user microSD card in the slot (the OS slots live on it)"
        if st.running == "unknown":
            return "the harness cannot tell which slot it booted (no stage0 status block)"
        return ""

    def _annotate(self, st: SlotStatus) -> SlotStatus:
        slots = {}
        for name, info in st.slots.items():
            rec = self.records.get(info.hdr_crc) if info.valid else {}
            if rec:
                info = replace(info, image_sha256=str(rec.get("sha256", "")),
                               version=str(rec.get("version", "")))
            slots[name] = info
        return replace(st, slots=slots)

    def status(self) -> SlotStatus:
        raw = self._ask("status")
        if not raw.get("ok"):
            raise _reply_refusal("status", raw)
        return self._annotate(self._measured(parse_status(raw)))

    def _measured(self, st: SlotStatus) -> SlotStatus:
        """``st`` with this process's estimate of its card job (``JobMeter``)."""
        job = self.meter.estimate(self._session.candidate.board_id, st.job)
        return st if job is st.job else replace(st, job=job)

    def busy_job(self) -> SlotStatus | None:
        """The reset guard's read (``services.reset_guard``): the status while the card job
        is writing or verifying, else None. Nothing is sent to a harness without the slot
        verbs (``version`` says so first); a harness that does not answer has no job
        running (None); one whose control port is held cannot say (``HeldError``)."""
        try:
            live = self._live()
        except HeldError:
            raise
        except HarnessError:
            return None
        if live.version_busy:
            pass                  # busy mid-request: it cannot say what it is; ask the slots
        elif not live.version_ok or (live.impl != IMPL_LINUX
                                     and SLOT_FEATURE not in live.features):
            return None
        try:
            st = self.status()
        except HeldError:
            raise
        except HarnessError as exc:
            log.info("reset guard: no card job to read on %s: %s",
                     self._session.candidate.board_id, exc)
            return None
        return st if st.job.busy else None

    def timeouts(self, nbytes: int, *, write: bool = True) -> SlotTimeouts:
        """This push's (``write``) or verify's budget: ``slot_timeouts``, or the seams."""
        t = slot_timeouts(nbytes, write=write)
        if self.job_timeout_s is not None:
            t = replace(t, job_s=float(self.job_timeout_s))
        if self.push_stall_s is not None:
            t = replace(t, push_stall_s=float(self.push_stall_s))
        self.last_timeouts = t
        return t

    # -- the lock -------------------------------------------------------------------------------

    def claimed(self, st: SlotStatus | None = None) -> bool:
        """Is the board's SSH claimed (so mutations must come from the board itself)?"""
        from harness_manager.services import slot_health

        from .shell import identify_quietly

        known = slot_health.claimed(st) if st is not None else None
        if known is not None:                     # slot status says it (S2): nothing to ask
            return known
        shell = self._shell()
        if not _tunnelled(self._session):
            reply = identify_quietly(shell.host, 0.5)
            ssh = reply.ssh if reply is not None else None
            if isinstance(ssh, dict) and "claimed" in ssh:
                return bool(ssh.get("claimed"))
        # The lock probe: a rollback guarded with the current default is refused by an
        # unlocked board ("slot mismatch") and changes nothing (module docstring).
        st = st or self.status()
        known = slot_health.claimed(st)
        if known is not None:
            return known
        if not st.default:
            return False
        reply = self._ask("rollback", st.default)
        if reply.get("ok"):                       # cannot happen by the contract; say so loudly
            log.warning("the slot lock probe was ACCEPTED on %s: %s",
                        self._session.candidate.board_id, reply)
            return False
        return _locked(reply)

    def _route(self, what: str, claimed: bool | None = None) -> str:
        """The claim's route for a slot mutation ("" direct, "board-ssh"); a board this
        Harness Manager cannot enter is ``ClaimLockedError``, before anything is sent."""
        claim = getattr(self._session, "claim", None)
        if claim is None or not callable(getattr(claim, "lock_route", None)):
            return "board-ssh" if claimed else ""
        return claim.lock_route(what, impl=IMPL_LINUX, claimed=claimed)

    def _board_forward(self) -> Any:
        """``(host, control, push)`` through the session's claim forward (a ``with``)."""
        return claim_forward(self._session, "slot", "slot changes")

    def _mutate(self, act: str, slot: str | None) -> dict[str, Any]:
        """``commit``/``rollback``: through the board's SSH when the claim is ours; else
        direct, and again through the board's SSH if the board says it is locked."""
        if self._route(f"slot {act}") == "board-ssh":
            with self._board_forward() as (host, ctl, _push):
                reply = self._one(host, ctl, act, slot)
            self.last_route = "board-ssh"
            return reply
        reply = self._ask(act, slot)
        self.last_route = "direct"
        if not reply.get("ok") and _locked(reply):
            self._route(f"slot {act}", claimed=True)   # not ours: ClaimLockedError, the hint
            with self._board_forward() as (host, ctl, _push):
                reply = self._one(host, ctl, act, slot)
            self.last_route = "board-ssh"
        return reply

    # -- the verbs -----------------------------------------------------------------------------

    def commit(self, slot: str | None = None) -> SlotStatus:
        reply = self._mutate("commit", slot)
        if not reply.get("ok"):
            raise _reply_refusal("commit", reply)
        return self._annotate(parse_status(reply))

    def rollback(self, slot: str | None = None) -> SlotStatus:
        reply = self._mutate("rollback", slot)
        if not reply.get("ok"):
            raise _reply_refusal("rollback", reply)
        return self._annotate(parse_status(reply))

    def verify(self, slot: str | None = None, progress: Progress | None = None) -> SlotStatus:
        started = time.monotonic()
        reply = self._ask("verify", slot)
        if not reply.get("ok"):
            raise _reply_refusal("verify", reply)
        job = parse_status(reply).job
        budget = self.timeouts(job.length, write=False)
        st = self._wait_job(progress, phase="verify", before=None,
                            deadline=started + budget.job_s, stall_s=budget.push_stall_s)
        return st

    def push(self, image: Path, *, static_id: str, sha256: str = "", version: str = "",
             progress: Progress | None = None) -> SlotStatus:
        report: Progress = progress or (lambda phase, done, total: None)
        data = Path(image).read_bytes()
        facts = check_image(data)
        st = self.status()
        if not st.card:
            raise RefusedError("no user microSD card in the slot: nothing was pushed",
                               hint="insert the board's card (the OS slots live on it)")
        if st.job.state in ("writing", "verifying"):
            raise HeldError(f"the board's card is busy ({st.job.act} {st.job.slot} "
                            f"{st.job.state})", hint="wait for it, then try again")
        if not st.target:
            from harness_manager.services import slot_health

            bad = slot_health.fell_back(st)
            if bad:
                raise RefusedError(f"no free slot: slot {bad} failed to boot and stage0 went "
                                   f"back to {st.running}, which is not the default yet; "
                                   "nothing was pushed", hint=slot_health.fallback_hint(st))
            if st.pending_commit:
                raise RefusedError(f"no free slot: slot {st.pending_commit} is committed and "
                                   f"not booted yet; nothing was pushed", hint=RULE1_HINT)
            raise RefusedError(f"the board offers no slot to push into (running {st.running}, "
                               f"default {st.default or '?'}); nothing was pushed")
        if not static_id or not same_u32(static_id, st.fabric_sid):
            raise IncompatibleError(
                f"the image is provisioned for static {_hex32(static_id) or '(none)'}, the "
                f"board's fabric is {st.fabric_sid or '(unknown)'}: nothing was pushed",
                hint="the Ethernet door carries only an image for the running static; a new "
                     "static goes through the Debug USB or the hub")
        target = st.target
        locked = self._route("slot push", claimed=self.claimed(st)) == "board-ssh"
        budget = self.timeouts(len(data))
        sid = int(_hex32(static_id), 16)
        report("push", 0, len(data))
        started = time.monotonic()
        try:
            if locked:
                with self._board_forward() as (host, _ctl, push_port):
                    self._stream(lambda: pv_slot.push_slot_image(
                        data, host, static_id=sid, slot=target, via="tcp", port=push_port,
                        timeout_s=budget.push_stall_s), progress, target, st.job)
                self.last_route = "board-ssh"
            else:
                host, port = self._shell().host, self._push_port()
                self._stream(lambda: pv_slot.push_slot_image(
                    data, host, static_id=sid, slot=target, via="tcp", port=port,
                    timeout_s=budget.push_stall_s), progress, target, st.job)
                self.last_route = "direct"
        except pv_slot.SlotError as exc:
            raise RefusedError(f"the boot image was refused before sending: {exc}") from exc
        except PushError as exc:
            raise ActionFailedError(f"the push to slot {target} failed: {exc}; "
                                    f"{self._state_words()}", hint=_BUSY_HINT) from exc
        except OSError as exc:
            raise UnreachableError(f"the push to slot {target} failed: {exc}") from exc
        report("push", len(data), len(data))
        final = self._wait_job(progress, phase="readback", before=st.job, push_slot=target,
                               want_crc=facts["hdr_crc"], deadline=started + budget.job_s,
                               stall_s=budget.push_stall_s)
        got = final.slots.get(target)
        if final.staged != target or got is None or not same_u32(got.hdr_crc, facts["hdr_crc"]):
            raise ActionFailedError(
                f"slot {target} does not hold the image just pushed (staged "
                f"{final.staged or 'nothing'}, hdr_crc {got.hdr_crc if got else '?'} != "
                f"{facts['hdr_crc']})", hint="nothing was committed; try the push again")
        self.records.put(facts["hdr_crc"], sha256=sha256 or _sha256(data), version=version)
        return self._annotate(final)

    def _stream(self, send: Callable[[], Any], progress: Progress | None, target: str,
                before: SlotJob) -> None:
        """Run the push (``send``) and, while it streams, report the card's write from
        ``status`` (``got`` counts what reached the card). The board writes as it receives,
        so a 29 MB push streams for ~12 min. A status that fails meanwhile is skipped: the
        push's own stall limit is the judge of the transfer."""
        box: dict[str, BaseException] = {}

        def run() -> None:
            try:
                send()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                box["exc"] = exc

        worker = threading.Thread(target=run, name="slot-push", daemon=True)
        worker.start()
        t0 = time.monotonic()
        while True:
            worker.join(self._poll_every(time.monotonic() - t0))
            if not worker.is_alive():
                break
            try:
                st = self._measured(parse_status(self._must_ok(self._ask("status"), "status")))
            except HarnessError as exc:
                log.debug("status while pushing: %s", exc)
                continue
            job = st.job
            if job != before and job.act == "push" and job.slot == target and job.busy:
                self._report_job(progress, job.state, job)
        if "exc" in box:
            raise box["exc"]

    def _poll_every(self, elapsed: float) -> float:
        """``poll_s`` for the first 10 s of a job, then ``poll_max_s``."""
        return self.poll_s if elapsed < 10.0 else max(self.poll_s, self.poll_max_s)

    @staticmethod
    def _report_job(progress: Progress | None, phase: str, job: SlotJob) -> None:
        from harness_manager.services.slots import job_detail

        detail = job_detail(job) if job.busy else None
        report_progress(progress, phase, job.got, job.length, detail)

    def _wait_job(self, progress: Progress | None, *, phase: str, before: SlotJob | None,
                  push_slot: str = "", want_crc: str = "",
                  deadline: float | None = None, stall_s: float | None = None) -> SlotStatus:
        """Poll ``status`` until the card job is done. THE guard is ``stall_s``: the job's
        byte count has not moved for that long (a write always counts; a read-back only once
        the board has counted it); ``deadline`` (the whole job's cap) is the backstop.

        After a push (``before`` = the job before it): the board takes the header before
        the host's send completes, so the first status already shows the push's job. A
        push it closed UNREAD (the lock, a running job, a bad header) starts none: the job
        is unchanged and the slot is not staged with this image, which is refused at once.
        While the job runs, each report carries its rate and ETA (``_report_job``).
        """
        from harness_manager.services.slots import card_state_words

        t0 = time.monotonic()
        if deadline is None:
            deadline = t0 + (self.job_timeout_s if self.job_timeout_s is not None
                             else JOB_TIMEOUT_S)
        budget = deadline - t0
        started = before is None
        last: SlotStatus | None = None
        silent_since: float | None = None
        mark: tuple[str, int] | None = None       # (state, got) when the bytes last moved
        moved_at = t0
        counted = False                           # this phase counts its bytes
        while True:
            try:
                reply = self._ask("status")
            except (HeldError, UnreachableError) as exc:
                # One client at a time on 6900: a failed read now and then is not the card.
                # Reads only: nothing is ever sent again that writes.
                now = time.monotonic()
                silent_since = silent_since if silent_since is not None else now
                if now - silent_since < STATUS_GRACE_S and now < deadline:
                    time.sleep(self._poll_every(now - t0))
                    continue
                raise ActionFailedError(
                    f"the board stopped answering `slot status` during the card job "
                    f"({exc.message}); last seen: "
                    f"{card_state_words(last) if last else 'no status yet'}",
                    hint=_BUSY_HINT) from exc
            silent_since = None
            if not reply.get("ok"):
                err = str(reply.get("err", ""))
                raise ActionFailedError(
                    f"the card failed during the job: slot status answers {err!r}; last seen: "
                    f"{card_state_words(last) if last else 'no status yet'}", hint=_BUSY_HINT)
            st = last = self._measured(parse_status(reply))
            job = st.job
            if not started:
                have = st.slots.get(push_slot)
                started = (job != before and job.act == "push") or (
                    st.staged == push_slot and have is not None
                    and same_u32(have.hdr_crc, want_crc))
                if not started:
                    raise RefusedError(
                        f"the board closed the push to slot {push_slot} unread (the board "
                        "is claimed and this is not its SSH, a card job was running, or "
                        "the header was refused): nothing was written",
                        hint="`harness-manager slot status TARGET` shows the card job")
            if started:
                self._report_job(progress, phase, job)
                if job.state == "failed":
                    raise _job_failure(job, st)
                if job.state not in ("writing", "verifying"):
                    return st
                now = time.monotonic()
                if mark is None or mark[0] != job.state:
                    counted, mark, moved_at = job.state == "writing", (job.state, job.got), now
                elif job.got != mark[1]:
                    counted, mark, moved_at = True, (job.state, job.got), now
                elif stall_s and counted and now - moved_at >= stall_s:
                    raise ActionFailedError(
                        f"the card job made no progress for {now - moved_at:.0f} s (stuck "
                        f"{job.state}); {card_state_words(st)}",
                        hint=_BUSY_HINT + f" A longer stall limit: the setting {STALL_KEY}")
            if time.monotonic() >= deadline:
                raise ActionFailedError(
                    f"the card job is still {job.state} after its {budget:.0f} s cap; "
                    f"{card_state_words(st)}",
                    hint=_BUSY_HINT + f" A longer budget: the setting {JOB_TIMEOUT_KEY}, or "
                         f"the card's measured rates ({CARD_WRITE_BPS_KEY}, "
                         f"{CARD_READ_BPS_KEY})")
            time.sleep(self._poll_every(time.monotonic() - t0))

    def _state_words(self) -> str:
        """The card's state for an error message: one ``status`` read, never a write."""
        from harness_manager.services.slots import card_state_words

        try:
            return card_state_words(self.status())
        except HarnessError as exc:
            return f"the card's state cannot be read ({exc.message})"

    @staticmethod
    def _must_ok(reply: dict[str, Any], act: str) -> dict[str, Any]:
        if not reply.get("ok"):
            raise _reply_refusal(act, reply)
        return reply

    # -- reboot ---------------------------------------------------------------------------------

    def reboot(self, progress: Progress | None = None, wait_s: float = 180.0) -> dict | None:
        """The ``reboot`` verb, witnessed: the harness's ``up_ms`` (from ``stats``) must
        restart after the request, i.e. be shorter than the time since it was sent."""
        from harness_manager.services import reset_guard

        report: Progress = progress or (lambda p, d, t: None)
        shell = self._shell()
        # SLOT-TIMING: never while the card job writes or reads back (a reset wedged it).
        reset_guard.check(self._session, reset_guard.ACTION_HARNESS_REBOOT)
        try:
            resp = shell.call(lambda c: c.reboot())
        except HeldError as exc:
            busy = card_job_refusal(self._session, exc)
            if busy is None:
                raise
            raise busy from exc
        if not resp.ok:
            raise ActionFailedError(f"the harness refused reboot: {resp.err or '?'}",
                                    hint="it needs the watchdog ('reboot' feature)")
        sent = time.monotonic()
        report("reboot", 0, int(wait_s))
        down_after: float | None = None
        down_evidence = ""
        while True:
            elapsed = time.monotonic() - sent
            try:
                stats = shell.call(lambda c: c.stats())
                up_ms = int(getattr(stats, "up_ms", 0) or 0)
            except HarnessError as exc:
                if down_after is None:
                    down_after, down_evidence = elapsed, f"the harness stopped answering ({exc})"
                up_ms = None
            if up_ms is not None and up_ms < elapsed * 1000.0 and elapsed * 1000.0 >= resp.in_ms:
                witness = {"down_after_s": round(down_after if down_after is not None
                                                 else resp.in_ms / 1000.0, 3),
                           "up_after_s": round(elapsed, 3),
                           "down_evidence": down_evidence or
                           f"up_ms restarted (reboot answered in_ms={resp.in_ms})",
                           "up_evidence": f"stats answers, up_ms {up_ms} < {elapsed * 1000:.0f} "
                                          "ms since the request",
                           "summary": f"reboot witnessed: up again after {elapsed:.1f} s"}
                self.last_reboot = witness
                report("reboot", int(wait_s), int(wait_s))
                return witness
            if elapsed >= wait_s:
                raise ActionFailedError(
                    f"the reboot was not witnessed within {wait_s:.0f} s ("
                    + ("it went down and did not come back" if down_after is not None
                       else "the harness never restarted") + ")",
                    hint="check the board: `harness-manager info TARGET`")
            report("reboot", int(elapsed), int(wait_s))
            time.sleep(self.reboot_poll_s)


def card_job_refusal(session: Any, exc: HeldError) -> HarnessError | None:
    """The harness answered ``reboot`` with ``EBUSY`` (``exc``): harnessd from platform 53f49b4
    on does so while a card job writes or verifies, as well as mid-swap. When the card job is
    what refused it (read again now: one that started after the reset guard read the card,
    say another host's push), the refusal in the guard's words (``CardBusyError``, naming the
    job); None for anything else (a swap, another client, a job that cannot be read), which
    the caller raises as it was."""
    from harness_manager.services import reset_guard

    if not getattr(exc, "ebusy", False) or isinstance(exc, reset_guard.CardBusyError):
        return None
    try:
        st = reset_guard.busy_job(session)
    except HarnessError:
        return None
    if st is None:
        return None
    return reset_guard.refusal(reset_guard.ACTION_HARNESS_REBOOT, st.job)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _tunnelled(session: Any) -> bool:
    """Reached through a TCP-only tunnel (UDP identify cannot cross it)."""
    from .deploy import is_tunnelled

    return is_tunnelled(session) or getattr(session, "reach", None) is not None


def _refusal(act: str, err: str, code: str = "", st: SlotStatus | None = None) -> HarnessError:
    """The board refused a ``slot`` act: the error class its ``code`` (else its reason)
    means (``slot_words.refusal``)."""
    return slot_words.refusal(act, err, code, st)


#: What to do when a card job went wrong: never a second write over a running one.
_BUSY_HINT = ("nothing was sent again: `harness-manager slot status TARGET` shows the card job; "
              "do not push again or reset the board while it says writing or verifying (a "
              "reset mid-write can wedge the card; only if it never ends, an MCC power cycle)")


def _locked(reply: dict[str, Any]) -> bool:
    """The claim lock's refusal (by its ``code`` when sent, else its text)."""
    return slot_words.is_claim_lock(str(reply.get("err", "")), slot_words.reply_code(reply))


def _reply_refusal(act: str, reply: dict[str, Any]) -> HarnessError:
    return _refusal(act, str(reply.get("err", "")), slot_words.reply_code(reply))


def _job_failure(job: SlotJob, st: SlotStatus | None = None) -> HarnessError:
    """A failed card job; its ``job.code`` (in the reply ``st`` came from) when sent."""
    from harness_manager.services.slots import card_state_words

    code = slot_words.reply_code((st.raw or {}).get("job")) if st is not None else ""
    return slot_words.job_failure(job, code, st,
                                  extra=card_state_words(st) if st is not None else "")


def make_os_slot_adapter(session: Any) -> Mps3OsSlots | None:
    """The pack hook (CCR T7-2): an adapter for any session with an Ethernet shell. Whether
    the harness can use it now is ``slots_reason()`` (a Linux harness with a card)."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3OsSlots(session)
