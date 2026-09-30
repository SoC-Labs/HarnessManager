"""The Linux harness's OS slots and the board's user microSD, for the front-ends (lane LINUX-SLOTS).

Board-agnostic: everything board-specific is the pack's ``session.os_slots``
(``core.pack.OsSlotAdapter``, CCR T7-2) and ``session.card`` (``CardAdapter``, CCR LS-1).
The CLI (``harness-manager slot|card``) and the daemon read through here.

Rules kept here, whatever the pack:

- **reads are open**: ``slot status`` and ``card status`` need no lease;
- **every change needs the lease** (``lease_check``, the update service's gate: a board
  behind a hub is changed only by the lease holder; a board with no hub has no lease)
  and the front-end's confirm;
- an adapter that cannot be used now says why (``slots_reason``/``card_reason``) and
  that is ``UnavailableError`` (exit 12) before anything is sent: a bare-metal harness
  has no OS slots, a harness without the ``usd`` feature has no card store;
- **no card -> the boot is unchanged** (david): every card change refuses cleanly, and
  the status says the board boots as it always has;
- **rule 1**: after a commit that was not booted there is no free slot; ``push`` refuses
  that unless asked to roll the commit back first (``rollback_first``);
- **the image's static, never the board's**: a push names the static the image was
  provisioned for (``linux_bundle.json`` ``targets.ethernet.provisioned.static_id``);
  the board refuses any other, and so does this service, before sending a byte;
- **rollback** after booting a healthy but wrong image: verify the other slot, make it
  the default, reboot, and check the board runs it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.pack import (
    TAKES_DETAIL,
    CardStatus,
    Progress,
    SlotInfo,
    SlotJob,
    SlotStatus,
)

OS_CAPABILITY = "OS slot update"
CARD_CAPABILITY = "user microSD"
LINUX_BUNDLE = "linux_bundle.json"
SLOT_IMAGE = "linux_slot.img"
REBOOT_WAIT_S = 180.0

LeaseCheck = Callable[[Any, str], Any]


@dataclass(frozen=True)
class PushSource:
    """What a push sends, and the static it is for (from a bundle, or given)."""

    image: Path
    static_id: str
    sha256: str = ""
    version: str = ""
    frames: dict[str, Any] | None = None      # linux_bundle.json slot_image.s0lb


def push_source(image: Path | None, *, bundle: Path | None = None, static_id: str = "",
                version: str = "") -> PushSource:
    """Resolve a push: ``bundle`` (a ``linux_bundle.json`` or its directory) names the
    provisioned static, the image's sha256 and frames, and ``linux_slot.img`` beside it;
    ``static_id`` alone names the static for a bare image. Refuses an image the bundle
    does not describe (sha256) and a push with no static (never the board's own)."""
    import hashlib

    from harness_manager.services.update import s0lb

    doc: dict[str, Any] = {}
    if bundle is not None:
        path = bundle / LINUX_BUNDLE if bundle.is_dir() else bundle
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read {path}: {exc}") from exc
        if doc.get("schema") != "mps3-linux-bundle" or str(doc.get("schema_version")) != "1":
            raise UsageError(f"{path} is not an mps3-linux-bundle v1")
        if image is None:
            image = path.parent / SLOT_IMAGE
    if image is None:
        raise UsageError("which image? give IMAGE, or --bundle with linux_slot.img beside it")
    try:
        data = Path(image).read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read {image}: {exc}") from exc
    sha = hashlib.sha256(data).hexdigest()
    eth = ((doc.get("targets") or {}).get("ethernet") or {}) if doc else {}
    prov = str(((eth.get("provisioned") or {}).get("static_id")) or "")
    si = eth.get("slot_image") or {}
    if doc:
        if si.get("sha256") and si["sha256"].lower() != sha:
            raise RefusedError(f"{Path(image).name} is not the image {LINUX_BUNDLE} describes "
                               f"(sha256 {sha[:12]}…, the bundle says {si['sha256'][:12]}…)",
                               hint="use the bundle's own linux_slot.img; nothing was pushed")
        if static_id and prov and int(static_id, 16) != int(prov, 16):
            raise UsageError(f"--static-id {static_id} contradicts the bundle's provisioned "
                             f"static {prov}")
        if doc.get("fieldable") is False:
            raise RefusedError("the bundle is not fieldable (a prototype): it is never pushed "
                               "to a board (FLOW_CONTRACT §5)")
    sid = static_id or prov
    if not sid:
        raise UsageError("the static the image was provisioned for is not known",
                         hint="give --bundle linux_bundle.json (its targets.ethernet."
                              "provisioned.static_id), or --static-id; the board's own static "
                              "is never assumed")
    try:
        sid = f"0x{int(sid, 16) & 0xFFFFFFFF:08x}"
    except ValueError as exc:
        raise UsageError(f"{sid!r} is not a 32-bit static_id") from exc
    frames = si.get("s0lb") if isinstance(si.get("s0lb"), dict) else None
    if frames:
        try:
            diff = s0lb.compare(s0lb.parse(data), frames)
        except s0lb.S0lbError as exc:
            raise RefusedError(f"{Path(image).name} is not a boot image stage0 would take: "
                               f"{exc}") from exc
        if diff:
            raise RefusedError(f"{Path(image).name}'s frames are not the bundle's: "
                               + "; ".join(diff), hint="nothing was pushed")
    ver = version or str(doc.get("harness") or ((eth.get("components") or {}).get("harness"))
                         or "")
    return PushSource(image=Path(image), static_id=sid, sha256=sha, version=ver, frames=frames)


class SlotService:
    """``slot`` and ``card`` for one board session at a time (module docstring)."""

    def __init__(self, *, lease_check: LeaseCheck | None = None, bus: EventBus | None = None,
                 store: Any = None) -> None:
        self.lease_check = lease_check
        self.bus = bus
        self.store = store

    # -- plumbing ----------------------------------------------------------------------------

    def _emit(self, topic: str, board_id: str, **data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    def _lease(self, session: Any, what: str) -> None:
        if self.lease_check is not None:
            self.lease_check(session, what)

    def check_lease(self, session: Any, what: str) -> None:
        """UI2 G6: the lease rule alone, so a front-end can refuse (409 HELD, naming the
        holder) before it starts a job; every change checks it again itself."""
        self._lease(session, what)

    def _progress(self, board_id: str, topic: str) -> Progress:
        def emit(phase: str, done: int, total: int, detail: dict[str, Any] | None = None) -> None:
            # SLOT-TIMING: a long card job adds rate_bps, eta_s and its one-line text.
            self._emit(f"{topic}.progress", board_id, phase=phase, bytes=done, total=total,
                       **(detail or {}))
        setattr(emit, TAKES_DETAIL, True)
        return emit

    @staticmethod
    def slots(session: Any) -> Any:
        adapter = getattr(session, "os_slots", None)
        if adapter is None:
            raise UnavailableError(OS_CAPABILITY, "this board pack or link has no OS slots")
        reason = adapter.slots_reason()
        if reason:
            raise UnavailableError(OS_CAPABILITY, reason)
        return adapter

    def card(self, session: Any) -> Any:
        adapter = getattr(session, "card", None)
        if adapter is None:
            raise UnavailableError(CARD_CAPABILITY, "this board pack or link has no user card")
        reason = adapter.card_reason()
        if reason:
            raise UnavailableError(CARD_CAPABILITY, reason)
        deploy = getattr(session, "deploy", None)
        if self.store is not None and hasattr(deploy, "use_store"):
            deploy.use_store(self.store)       # the card commit re-pushes from the store
        return adapter

    # -- OS slots ----------------------------------------------------------------------------

    def status(self, session: Any) -> SlotStatus:
        return self.slots(session).status()

    def push(self, session: Any, source: PushSource, *, rollback_first: bool = False,
             progress: Progress | None = None) -> dict[str, Any]:
        """Push into the free slot and read it back (NOT committed: ``commit`` does that)."""
        adapter = self.slots(session)
        bid = session.candidate.board_id
        self._lease(session, "push an OS image to the board's card")
        before = adapter.status()
        undone = ""
        if not before.target and before.pending_commit and rollback_first:
            undone = before.pending_commit
            before = adapter.rollback()
        self._emit("slot.started", bid, act="push", image=source.image.name)
        st = adapter.push(source.image, static_id=source.static_id, sha256=source.sha256,
                          version=source.version,
                          progress=progress or self._progress(bid, "slot"))
        self._emit("slot.done", bid, act="push", slot=st.staged)
        return {"act": "push", "slot": st.staged, "rolled_back_first": undone, "status": st}

    def commit(self, session: Any, slot: str | None = None) -> dict[str, Any]:
        adapter = self.slots(session)
        self._lease(session, "commit an OS slot")
        st = adapter.commit(slot)
        self._emit("slot.done", session.candidate.board_id, act="commit", slot=st.default)
        return {"act": "commit", "slot": st.default, "status": st,
                "note": f"slot {st.default} boots at the next reboot"}

    def verify(self, session: Any, slot: str | None = None,
               progress: Progress | None = None) -> dict[str, Any]:
        adapter = self.slots(session)
        st = adapter.verify(slot, progress=progress)
        return {"act": "verify", "slot": st.job.slot or slot or "", "status": st}

    def rollback(self, session: Any, *, reboot: bool = True, wait_s: float = REBOOT_WAIT_S,
                 progress: Progress | None = None) -> dict[str, Any]:
        """Make the other slot the default again.

        - a commit that was not booted (default != running): the running slot becomes the
          default again; nothing reboots;
        - the board booted the new image (default == running): the other slot is verified
          (read back off the card), made the default, and, unless ``reboot`` is False, the
          board reboots into it and must be running it after.
        """
        adapter = self.slots(session)
        bid = session.candidate.board_id
        self._lease(session, "roll the OS slot back")
        st = adapter.status()
        if st.pending_commit:
            after = adapter.rollback(st.running)
            self._emit("slot.done", bid, act="rollback", slot=after.default)
            return {"act": "rollback", "slot": after.default, "rebooted": False, "status": after,
                    "note": f"the commit of slot {st.pending_commit} is undone; slot "
                            f"{after.default} stays the default"}
        other = "B" if st.running == "A" else "A"
        info = st.slots.get(other)
        if info is None or not info.valid:
            raise RefusedError(f"slot {other} holds no valid image to roll back to "
                               f"({info.state if info else 'absent'})",
                               hint="push a known-good image instead")
        if info.verified == "no":
            adapter.verify(other, progress=progress)
        after = adapter.rollback(other)
        out: dict[str, Any] = {"act": "rollback", "slot": after.default, "rebooted": False,
                               "status": after}
        if not reboot:
            out["note"] = f"slot {after.default} boots at the next reboot"
            return out
        evidence = adapter.reboot(progress=progress, wait_s=wait_s)
        final = adapter.status()
        out.update(rebooted=True, evidence=evidence, status=final)
        if final.running != other:
            raise ActionFailedError(f"after the reboot the board runs slot {final.running}, "
                                    f"not {other}", hint="`harness-manager slot status TARGET`")
        self._emit("slot.done", bid, act="rollback", slot=other)
        return out

    # -- the user microSD ----------------------------------------------------------------------

    def card_status(self, session: Any) -> CardStatus:
        return self.card(session).status()

    def card_commit(self, session: Any, progress: Progress | None = None) -> dict[str, Any]:
        adapter = self.card(session)
        bid = session.candidate.board_id
        self._lease(session, "commit the running overlay to the card")
        out = adapter.commit(progress=progress or self._progress(bid, "card"))
        self._emit("card.done", bid, act="commit", **{k: v for k, v in out.items()
                                                       if isinstance(v, (str, int))})
        return out

    def card_clear(self, session: Any) -> CardStatus:
        adapter = self.card(session)
        self._lease(session, "clear the card's power-on default")
        st = adapter.clear()
        self._emit("card.done", session.candidate.board_id, act="clear")
        return st


# --- a long card job, in words (SLOT-TIMING) -------------------------------------------------------


def mb(n: int | float) -> str:
    """Bytes as MB (10^6), one decimal, no ".0": 12300000 -> "12.3", 29000000 -> "29"."""
    text = f"{n / 1e6:.1f}"
    return text[:-2] if text.endswith(".0") else text


def eta_text(eta_s: float | None) -> str:
    """"~6 min left" (never a false precision); "" when there is no estimate."""
    if eta_s is None:
        return ""
    if eta_s < 60:
        return "under a minute left"
    minutes = round(eta_s / 60)
    if minutes < 90:
        return f"~{minutes} min left"
    return f"~{minutes // 60} h {minutes % 60} min left"


def job_text(job: SlotJob) -> str:
    """The card job in one line: "writing slot B: 12.3 MB / 29 MB, ~6 min left". A read-back
    whose bytes the board does not count says its size: "verifying slot B (29 MB), ...". ""
    when no job runs."""
    if not job.busy:
        return ""
    where = f"slot {job.slot}" if job.slot else "the card"
    what = f"{job.state} {where}"
    if job.state == "writing" or 0 < job.got < job.length:
        what += f": {mb(job.got)} MB / {mb(job.length)} MB"
    elif job.length:
        what += f" ({mb(job.length)} MB)"
    left = eta_text(job.eta_s)
    return f"{what}, {left}" if left else what


def card_state_words(st: SlotStatus) -> str:
    """The card as an error message says it: "card: running A, default A, A valid, B empty;
    job push B writing 12.3 MB / 29 MB"."""
    if not st.card:
        return f"card: none answered (running {st.running})"
    slots = ", ".join(f"{n} {i.state}" + (f" ({i.err})" if i.err else "")
                      for n, i in sorted(st.slots.items()))
    j = st.job
    if j.act == "none" and j.state == "idle":
        job = "no job"
    else:
        job = f"job {j.act} {j.slot or '-'} {j.state}"
        if j.busy or j.state == "failed":
            job += f" {mb(j.got)} MB / {mb(j.length)} MB" if j.length else ""
        job += f" ({j.err})" if j.err else ""
    return (f"card: running {st.running}, default {st.default or '?'}, {slots}; {job}")


def job_detail(job: SlotJob) -> dict[str, Any]:
    """The ``detail`` of a progress call for this job (``core.pack.report_progress``)."""
    return {"slot": job.slot, "rate_bps": round(job.rate_bps), "eta_s":
            None if job.eta_s is None else round(job.eta_s), "text": job_text(job)}


def slot_status_json(st: SlotStatus) -> dict[str, Any]:
    """What the CLI and the API show of a ``SlotStatus``."""
    return {
        "card": st.card, "running": st.running, "default": st.default, "target": st.target,
        "staged": st.staged, "fabric_sid": st.fabric_sid, "seq": st.seq,
        "pending_commit": st.pending_commit,
        "slots": {n: {"state": s.state, "hdr_crc": s.hdr_crc, "len": s.length, "sid": s.sid,
                      "verified": s.verified, "err": s.err, "image_sha256": s.image_sha256,
                      "version": s.version, "running": n == st.running,
                      "default": n == st.default}
                  for n, s in sorted(st.slots.items())},
        "job": {"act": st.job.act, "slot": st.job.slot, "state": st.job.state,
                "got": st.job.got, "len": st.job.length, "err": st.job.err,
                # SLOT-TIMING (additive): busy = no reset now; the pack's estimates
                "busy": st.job.busy, "rate_bps": round(st.job.rate_bps),
                "eta_s": None if st.job.eta_s is None else round(st.job.eta_s),
                "text": job_text(st.job)},
    }


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def slot_status_from_json(doc: dict[str, Any]) -> SlotStatus:
    """``slot_status_json`` (plus ``slot_health.extend_json``'s keys) back to a ``SlotStatus``:
    what the CLI reads from the service's ``GET /slots`` and ``GET /card`` (SERIAL-6900 4a).
    ``raw`` keeps the board's own words the view reads (``confirmed``, ``claimed``)."""
    slots: dict[str, SlotInfo] = {}
    for name, one in (doc.get("slots") or {}).items():
        if not isinstance(one, dict):
            continue
        slots[str(name)] = SlotInfo(
            name=str(name), state=_text(one.get("state")), hdr_crc=_text(one.get("hdr_crc")),
            length=_int(one.get("len")), sid=_text(one.get("sid")),
            verified=_text(one.get("verified")) or "no", err=_text(one.get("err")),
            image_sha256=_text(one.get("image_sha256")), version=_text(one.get("version")))
    job = doc.get("job") if isinstance(doc.get("job"), dict) else {}
    rate = job.get("rate_bps")
    eta = job.get("eta_s")
    raw = {k: doc[k] for k in ("confirmed", "claimed") if isinstance(doc.get(k), bool)}
    return SlotStatus(
        running=_text(doc.get("running")) or "unknown", slots=slots,
        card=bool(doc.get("card", False)), default=_text(doc.get("default")),
        target=_text(doc.get("target")), staged=_text(doc.get("staged")),
        fabric_sid=_text(doc.get("fabric_sid")), seq=_int(doc.get("seq")),
        job=SlotJob(act=_text(job.get("act")) or "none", slot=_text(job.get("slot")),
                    state=_text(job.get("state")) or "idle", got=_int(job.get("got")),
                    length=_int(job.get("len")), err=_text(job.get("err")),
                    rate_bps=float(rate) if isinstance(rate, (int, float))
                    and not isinstance(rate, bool) else 0.0,
                    eta_s=float(eta) if isinstance(eta, (int, float))
                    and not isinstance(eta, bool) else None),
        raw=raw)


def card_status_from_json(doc: dict[str, Any]) -> CardStatus:
    """``card_status_json`` back to a ``CardStatus`` (the service's ``GET /card``: SERIAL-6900
    4a), with its OS slots."""
    default = doc.get("default")
    mb = doc.get("card_mb")
    os_doc = doc.get("os_slots")
    return CardStatus(
        store=bool(doc.get("store", False)), present=bool(doc.get("present", False)),
        state=_text(doc.get("state")), text=_text(doc.get("text")),
        reason=_text(doc.get("reason")),
        card_mb=mb if isinstance(mb, int) and not isinstance(mb, bool) else None,
        default={str(k): _text(v) for k, v in default.items()} if isinstance(default, dict)
        else None,
        boot=_text(doc.get("boot")), committable=bool(doc.get("committable", False)),
        os_slots=slot_status_from_json(os_doc) if isinstance(os_doc, dict) else None,
        notes=tuple(str(n) for n in doc.get("notes") or ()))


def card_status_json(st: CardStatus) -> dict[str, Any]:
    """What the CLI and the API show of a ``CardStatus``."""
    return {"store": st.store, "present": st.present, "state": st.state, "text": st.text,
            "reason": st.reason, "card_mb": st.card_mb,
            "default": st.default, "boot": st.boot, "committable": st.committable,
            "os_slots": slot_status_json(st.os_slots) if st.os_slots is not None else None,
            "notes": list(st.notes), "line": card_line(st)}


def card_line(st: CardStatus | None, reason: str = "") -> str:
    """One line for the Board tile: present / store / default / OS slots."""
    if st is None:
        return reason or "unknown"
    if not st.store:
        return f"n/a: {st.reason or 'no card store'}"
    if not st.present:
        return "none (boots as always)"
    bits = [st.state or "?"]
    if st.default:
        name = st.default.get("rm_name") or st.default.get("rm_id") or "?"
        bits.append(f"default {name} [{st.default.get('slot') or '?'}]")
    if st.os_slots is not None:
        os = st.os_slots
        if os.job.busy:                    # SLOT-TIMING: the job first, while it runs
            return job_text(os.job)
        slots = " ".join(f"{n}:{i.state}" + ("*" if n == os.running else "")
                         for n, i in sorted(os.slots.items()))
        bits.append(f"OS {slots}")
    return " · ".join(bits)


def lease_check_for(engine: Any, leases: Any = None) -> LeaseCheck:
    """The install lease gate (``update.lease_gate``) for ``engine``'s boards: open for a
    board with no hub; the lease holder only behind one. ``leases`` is a shared
    ``LeaseService`` (the daemon's); otherwise one is made on first need."""
    from harness_manager.services.update.lease_gate import lease_state, require_lease

    shared: list[Any] = [leases]

    def check(session: Any, what: str) -> dict[str, Any]:
        if getattr(session, "hub", None) is None:
            return lease_state(session, None)
        if shared[0] is None:
            from harness_manager.services.lease import LeaseService
            from harness_manager.services.update.service import _state_dir

            shared[0] = LeaseService(_state_dir(engine, None))
        return require_lease(session, shared[0], what)

    return check
