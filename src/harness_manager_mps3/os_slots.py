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

- sends ``commit``/``rollback`` direct; on ``slot locked: board claimed (use ssh)`` it
  sends the SAME request again through an SSH forward to the board's 127.0.0.1
  (a locked refusal changes nothing, so the retry is safe);
- decides the push route BEFORE pushing, because a locked push is closed unread and
  says nothing: by ``identify.ssh.claimed`` (UDP 6899) when identify answers; when it
  cannot (UDP does not cross the hub's SSH tunnel), by a LOCK PROBE: ``rollback``
  guarded with the current default slot. A rollback always picks the slot that is not
  the default, so an unlocked board refuses the guard (``slot mismatch``) and changes
  nothing, while a locked one answers the lock refusal. When ``slot status`` carries
  ``claimed`` (the Linux lead's S2, additive) that answers, and neither identify nor the
  probe is asked.

The board-SSH forward is ``ssh -J HUB USER@BOARD -L ...:127.0.0.1:6900 -L ...:6910``
(``tunnel.SshTunnel``, the same reach as XVC's; boards.toml ``xvc = {user, host}``
names the login). It is opened per mutation and closed after it.

**Rule 1.** After a ``commit`` and before the reboot the board has no push target.
``push`` refuses that locally with the fix (``slot rollback``); the update executor
rolls back itself before a second push (HARNESS_DISTRIBUTION §9 item 8).

**What this host pushed.** The board knows an image by its S0LB table CRC
(``hdr_crc``), not by a sha256 or a release. The adapter keeps, per board, which
image (sha256, release version) it pushed under which ``hdr_crc``
(``<state>/slots/<board>.json``), so ``status`` can name what each slot holds.

Test seams: ``poll_s``/``job_timeout_s``/``reboot_poll_s`` on the adapter; the push port
follows the deploy adapter's (``HARNESS_MANAGER_MPS3_PUSH_PORT``); the board-SSH
forward uses ``tunnel.DEFAULT_LAUNCHER``/``DEFAULT_SSH_G``; identify uses
``HARNESS_MANAGER_MPS3_IDENTIFY_PORT``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import zlib
from collections.abc import Iterator
from dataclasses import replace
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
from harness_manager.core.model import LinkKind
from harness_manager.core.pack import Progress, SlotInfo, SlotJob, SlotStatus

from . import slot_words
from .constants import CONTROL_PORT, IMPL_LINUX, PUSH_PORT

log = logging.getLogger(__name__)

CAPABILITY = "OS slot update"
#: The feature name HM asked the Linux lane for (HARNESS_DISTRIBUTION §9 item 5).
SLOT_FEATURE = "slot"
SLOT_LOCKED_ERR = pv_slot.SLOT_LOCKED_ERR
#: How the two engines decline a verb they do not have.
_NO_VERB = ("unknown op", "slot not supported")
#: Poll the card job every 0.5-1 s, 180 s budget (HARNESS_DISTRIBUTION §9 item 3).
POLL_S = 0.5
JOB_TIMEOUT_S = 180.0
#: A reboot witness polls this often, and gives up after the caller's budget.
REBOOT_POLL_S = 0.5
#: The per-chunk stall limit of the kind-2 push (pyverify's rule: never the whole transfer).
PUSH_STALL_S = 30.0
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


def board_ssh_target(session: Any) -> tuple[str, str, str]:
    """``(host, user, jump)`` for ``ssh -J JUMP USER@HOST``: the Linux harness's SSH, as the
    XVC board-SSH reach finds it (boards.toml ``xvc = {user, host}``, the candidate's SSH
    link, the hub it is reached through)."""
    from .xvc import DEFAULT_BOARD_USER, _jump_host, _ssh_link, xvc_config

    cand = session.candidate
    cfg = xvc_config(cand)
    link_user, link_host = _ssh_link(cand)
    host = cfg.get("host") or link_host or getattr(getattr(session, "reach", None),
                                                    "remote_host", "") or ""
    if not host:
        eth = next((lk for lk in cand.links if lk.kind == LinkKind.ETHERNET), None)
        if eth is not None:
            from .shell import parse_endpoint

            host = parse_endpoint(eth.address, CONTROL_PORT)[0]
    return host, cfg.get("user") or link_user or DEFAULT_BOARD_USER, _jump_host(cand)


# --- the adapter --------------------------------------------------------------------------------


class Mps3OsSlots:
    """``core.pack.OsSlotAdapter`` for an MPS3 session (module docstring)."""

    def __init__(self, session: Any, *, poll_s: float = POLL_S,
                 job_timeout_s: float = JOB_TIMEOUT_S, reboot_poll_s: float = REBOOT_POLL_S,
                 records: SlotRecords | None = None) -> None:
        self._session = session
        self.poll_s = poll_s
        self.job_timeout_s = job_timeout_s
        self.reboot_poll_s = reboot_poll_s
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
                        "from rescue is not built yet (HARNESS-DIST L3)")
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
        return self._annotate(parse_status(raw))

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

    @contextlib.contextmanager
    def _board_forward(self) -> Iterator[tuple[str, int, int]]:
        """``(host, control, push)`` through the board's own SSH to its 127.0.0.1."""
        from . import tunnel as _tunnel

        host, user, jump = board_ssh_target(self._session)
        if not host:
            raise UnreachableError("no board address for the board-SSH forward",
                                   hint="set boards.toml xvc = { host = \"...\" }")
        label = (f"{self._session.candidate.board_id} slot: ssh "
                 f"{f'-J {jump} ' if jump else ''}{user}@{host}")
        t = _tunnel.SshTunnel(host, [_tunnel.Forward("control", "127.0.0.1", CONTROL_PORT),
                                     _tunnel.Forward("push", "127.0.0.1", PUSH_PORT)],
                              jump=jump, user=user, label=label, restart=False)
        try:
            t.start()
        except UnreachableError as exc:
            raise UnreachableError(
                f"the board is claimed, so slot changes go through its SSH, and that did not "
                f"come up: {exc.message}",
                hint=f"check `ssh {f'-J {jump} ' if jump else ''}{user}@{host} true` works "
                     "without a prompt (your key is the claimed one)") from exc
        try:
            yield "127.0.0.1", t.local_port("control"), t.local_port("push")
        finally:
            t.close()

    def _mutate(self, act: str, slot: str | None) -> dict[str, Any]:
        """``commit``/``rollback``: direct; again through the board's SSH if it is locked."""
        reply = self._ask(act, slot)
        self.last_route = "direct"
        if not reply.get("ok") and _locked(reply):
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
        reply = self._ask("verify", slot)
        if not reply.get("ok"):
            raise _reply_refusal("verify", reply)
        st = self._wait_job(progress, phase="verify", before=None)
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
        locked = self.claimed(st)
        report("push", 0, len(data))
        try:
            if locked:
                with self._board_forward() as (host, _ctl, push_port):
                    pv_slot.push_slot_image(data, host, static_id=int(_hex32(static_id), 16),
                                            slot=target, via="tcp", port=push_port,
                                            timeout_s=PUSH_STALL_S)
                self.last_route = "board-ssh"
            else:
                pv_slot.push_slot_image(data, self._shell().host,
                                        static_id=int(_hex32(static_id), 16), slot=target,
                                        via="tcp", port=self._push_port(),
                                        timeout_s=PUSH_STALL_S)
                self.last_route = "direct"
        except pv_slot.SlotError as exc:
            raise RefusedError(f"the boot image was refused before sending: {exc}") from exc
        except PushError as exc:
            raise ActionFailedError(f"the push to slot {target} failed: {exc}",
                                    hint="the card keeps what it had; try again") from exc
        except OSError as exc:
            raise UnreachableError(f"the push to slot {target} failed: {exc}") from exc
        report("push", len(data), len(data))
        final = self._wait_job(progress, phase="readback", before=st.job, push_slot=target,
                               want_crc=facts["hdr_crc"])
        got = final.slots.get(target)
        if final.staged != target or got is None or not same_u32(got.hdr_crc, facts["hdr_crc"]):
            raise ActionFailedError(
                f"slot {target} does not hold the image just pushed (staged "
                f"{final.staged or 'nothing'}, hdr_crc {got.hdr_crc if got else '?'} != "
                f"{facts['hdr_crc']})", hint="nothing was committed; try the push again")
        self.records.put(facts["hdr_crc"], sha256=sha256 or _sha256(data), version=version)
        return self._annotate(final)

    def _wait_job(self, progress: Progress | None, *, phase: str, before: SlotJob | None,
                  push_slot: str = "", want_crc: str = "") -> SlotStatus:
        """Poll ``status`` until the card job is done.

        After a push (``before`` = the job before it): the board takes the header before
        the host's send completes, so the first status already shows the push's job. A
        push it closed UNREAD (the lock, a running job, a bad header) starts none: the job
        is unchanged and the slot is not staged with this image, which is refused at once.
        """
        report: Progress = progress or (lambda p, d, t: None)
        deadline = time.monotonic() + self.job_timeout_s
        started = before is None
        while True:
            st = parse_status(self._must_ok(self._ask("status"), "status"))
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
                report(phase, job.got, job.length)
                if job.state == "failed":
                    raise _job_failure(job, st)
                if job.state not in ("writing", "verifying"):
                    return st
            if time.monotonic() >= deadline:
                raise ActionFailedError(f"the card job is still {job.state} after "
                                        f"{self.job_timeout_s:.0f} s",
                                        hint="`harness-manager slot status TARGET` shows it")
            time.sleep(self.poll_s)

    @staticmethod
    def _must_ok(reply: dict[str, Any], act: str) -> dict[str, Any]:
        if not reply.get("ok"):
            raise _reply_refusal(act, reply)
        return reply

    # -- reboot ---------------------------------------------------------------------------------

    def reboot(self, progress: Progress | None = None, wait_s: float = 180.0) -> dict | None:
        """The ``reboot`` verb, witnessed: the harness's ``up_ms`` (from ``stats``) must
        restart after the request, i.e. be shorter than the time since it was sent."""
        report: Progress = progress or (lambda p, d, t: None)
        shell = self._shell()
        resp = shell.call(lambda c: c.reboot())
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


def _locked(reply: dict[str, Any]) -> bool:
    """The claim lock's refusal (by its ``code`` when sent, else its text)."""
    return slot_words.is_claim_lock(str(reply.get("err", "")), slot_words.reply_code(reply))


def _reply_refusal(act: str, reply: dict[str, Any]) -> HarnessError:
    return _refusal(act, str(reply.get("err", "")), slot_words.reply_code(reply))


def _job_failure(job: SlotJob, st: SlotStatus | None = None) -> HarnessError:
    """A failed card job; its ``job.code`` (in the reply ``st`` came from) when sent."""
    code = slot_words.reply_code((st.raw or {}).get("job")) if st is not None else ""
    return slot_words.job_failure(job, code, st)


def make_os_slot_adapter(session: Any) -> Mps3OsSlots | None:
    """The pack hook (CCR T7-2): an adapter for any session with an Ethernet shell. Whether
    the harness can use it now is ``slots_reason()`` (a Linux harness with a card)."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3OsSlots(session)
