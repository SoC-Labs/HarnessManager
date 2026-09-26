"""A Linux-harness board that also sends what the Linux lead proposed (lane LINUX-ANSWERS).

``AnswersBoard`` is ``tests.fakes.lxslots_board.SlotBoard`` (FakeShell's slot model, and a
reboot that boots stage0's pick) plus the ADDITIVE wire fields from the Linux lead's answers
(``HM_ANSWERS_2026-09-26.md``), each off unless asked for, so a test can show HM reads them
when present and never needs them:

- ``reports_confirmed``: ``slot status`` carries ``confirmed`` (S5): stage0's att_confirm
  for this boot, true after a boot that came up (``confirmed`` sets it by hand);
- ``reports_claimed``: ``slot status`` carries ``claimed`` (S2), the SSH claim;
- ``codes``: a refusal carries ``code`` beside ``err``, a failed job ``job.code`` (S3/C2),
  from the codes the Linux lead proposed, matched on today's texts.

``slot_acts`` records every ``slot`` act the board received (a test can prove nothing sent
a ``verify`` nobody asked for).
"""

from __future__ import annotations

from typing import Any

from tests.fakes.lxslots_board import LINUX_SID, SlotBoard

#: The Linux lead's proposed codes (S3), by the text the board sends today (prefix match).
VERB_CODES = (("slot locked", "locked"), ("EBUSY", "busy"), ("no card", "no_card"),
              ("card io", "card_io"), ("no stage0 block", "no_stage0"),
              ("fabric static_id unknown", "fabric_unknown"),
              ("nothing staged", "nothing_staged"), ("slot mismatch", "slot_mismatch"),
              ("bad act", "bad_act"), ("bad slot", "bad_slot"))
JOB_CODES = (("no slot record", "no_record"), ("no free slot", "no_free_slot"),
             ("image for", "wrong_static"), ("slot mismatch", "slot_mismatch"),
             ("image too large", "too_large"), ("torn", "torn"), ("aborted", "aborted"))


def code_for(err: str, table: tuple[tuple[str, str], ...]) -> str:
    if "identity lock:" in err:
        return "identity_lock"
    if err.endswith("not verified"):
        return "not_verified"
    if "changed since it was verified" in err:
        return "changed"
    if err.endswith("is not a valid image"):
        return "not_valid"
    return next((c for prefix, c in table if err.startswith(prefix)), "")


class AnswersBoard(SlotBoard):
    def __init__(self, *args: Any, reports_confirmed: bool = False,
                 reports_claimed: bool = False, codes: bool = False, confirmed: bool = True,
                 **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.reports_confirmed = reports_confirmed
        self.reports_claimed = reports_claimed
        self.codes = codes
        #: stage0's att_confirm for THIS boot (what ``confirmed`` reports)
        self.confirmed = confirmed
        self.slot_acts: list[str] = []

    def _simulate_restart(self) -> None:
        super()._simulate_restart()
        if self.slots is not None:
            self.confirmed = self.slots.running in ("A", "B")

    def _op_slot(self, request: dict[str, Any], peer: str | None) -> dict[str, Any]:
        self.slot_acts.append(str(request.get("act")))
        reply = super()._op_slot(request, peer)
        if self.codes and not reply.get("ok"):
            code = code_for(str(reply.get("err", "")), VERB_CODES)
            if code:
                reply["code"] = code
        if reply.get("ok"):
            if self.reports_confirmed:
                reply["confirmed"] = bool(self.confirmed)
            if self.reports_claimed:
                reply["claimed"] = bool(self.ssh_claimed)
            job = reply.get("job")
            if self.codes and isinstance(job, dict) and job.get("state") == "failed":
                code = code_for(str(job.get("err", "")), JOB_CODES)
                if code:
                    job["code"] = code
        return reply


def answers_board(**kw: Any) -> AnswersBoard:
    """A started ``AnswersBoard`` on ephemeral ports (``slot_board``'s defaults)."""
    kw.setdefault("profile", "linux")
    kw.setdefault("static_id", LINUX_SID)
    kw.setdefault("reboot_in_ms", 150)
    kw.setdefault("slots", {})
    kw.setdefault("harness_version", "1.0.0")
    fake = AnswersBoard.ephemeral(**kw)
    fake.start()
    return fake
