"""The HIL runbooks as data (lane HIL-AUTO): one ``Check`` per runbook check, grouped by the
runbook's own sections.

- ``linux`` / ``linux-netboot`` / ``linux-nocard``: ``docs/HIL_LINUX.md``. ``-netboot``: its
  "Netboot mode" preface (a blank card): the checks that touch the user microSD, its slots or
  the config SD are skipped, and C1/B3 expect what a netbooted board says. ``-nocard``: its
  "Card-less mode" preface (board 2, no user microSD at all): the same skips plus D4 and F6
  (no reset of any kind), and C1 expects Harness Manager's answer to the harness's
  ``card: false``: ``slot status`` exit 12, "no user microSD card in the slot";
- ``bare-metal``: ``docs/HIL_B0.md``.

A check's ``id`` is the runbook's (``A1``, ``R4``, ``0.3``). One runbook check that runs two
HM commands has a lettered follow-on (``R7b``: R7's ``identify``). Every expectation quotes
the runbook's **Expect** words (``said``) so a report reads like the runbook.
``tests/unit/test_hil_auto_plans.py`` parses the runbooks and fails when a check id is in one
and not the other.

``tier``:

- ``read``: runs with ``--writes none`` (the default). Reads the board, the hub or HM's state;
- ``safe``: only with ``--writes safe``: a swap that the runner puts back (program an overlay,
  verify, restore greybox), or a contact the soak may be using (the MCC read on ``tty_00``);
- ``manual``: never unattended: a person, a GUI, Vivado, a trust decision, or a write the
  runner must not make (card, slots, SD, MCC REBOOT, lease, claim). ``why`` says which.

``{B}`` in an argv is the board (``--board``); ``{static}`` the expected static id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

READ, SAFE, MANUAL = "read", "safe", "manual"

#: The runbooks' statics (``--expect-static`` overrides; a new mint changes them).
LINUX_STATIC = "0x44ee76d5"      # RC2, HIL_LINUX.md "The board"
BARE_METAL_STATIC = "0x72bb0a36"  # the ILA mint, HIL_B0.md "The board as of 09-24"
GREYBOX_RM = "0x00000000"


@dataclass(frozen=True)
class Expect:
    """One expected JSON field (``path``, dotted; ``list[key=value]`` picks an item) or, with
    ``path="$stdout"``, the command's text. ``op`` is one of ``OPS`` (``run.py``)."""

    path: str
    op: str
    value: Any = None
    said: str = ""
    #: "when the harness reports it (empty is not a failure)"
    optional: bool = False
    #: a failure is an unexpected identity: the run STOPS (exit 2) and restores greybox
    stop: bool = False


@dataclass(frozen=True)
class Answer:
    """Another answer a check accepts (lane HIL-IDLOC): exit ``exit`` checked against
    ``expects`` instead of the check's own. ``note`` is the pass reason in the report (A6 on
    an image without the harness feature ``locate``: "not on this image")."""

    exit: int
    expects: tuple[Expect, ...]
    note: str = ""


@dataclass(frozen=True)
class Check:
    id: str
    section: str
    title: str
    tier: str = READ
    #: the HM command after ``harness-manager``; ``--json`` is added unless ``text``
    argv: tuple[str, ...] = ()
    expects: tuple[Expect, ...] = ()
    #: the exit codes that are the expected answer (R5 on greybox: 13)
    exit_ok: tuple[int, ...] = (0,)
    #: human-output command (``board ssh -c``): no ``--json``; expectations use ``$stdout``
    text: bool = False
    #: what it changes on the board ("" for nothing)
    writes: str = ""
    #: the evidence file stem, from the runbook (``a1_info``)
    evidence: str = ""
    #: manual: why it is never unattended. read/safe: "" (runs)
    why: str = ""
    #: a skip reason in this plan (the netboot preface), or ""
    skip: str = ""
    #: a fact an earlier check recorded that must hold, else skipped with ``needs_why``
    needs: tuple[str, Any] | None = None
    needs_why: str = ""
    #: what to do when it fails (the runbook's failure table)
    hint: str = ""
    #: facts to record from the JSON: {fact: path}
    record: dict[str, str] = field(default_factory=dict)
    #: this is the MCC read on tty_00: another reader there is "skipped (tty_00 busy)"
    mcc: bool = False
    #: per-check timeout (s)
    timeout_s: float = 180.0
    #: other answers that pass, by exit code (A6: exit 12, the image has no ``locate``)
    answers: tuple[Answer, ...] = ()
    #: the pass reason in the report: ``{a.b}`` from the JSON, ``{fact:x}`` a recorded fact
    #: (A5: which image answered and what it said)
    note: str = ""

    @property
    def exits(self) -> tuple[int, ...]:
        """Every exit code that is an expected answer: ``exit_ok`` and the ``answers``'."""
        return self.exit_ok + tuple(a.exit for a in self.answers)


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class Plan:
    name: str
    runbook: str
    static: str
    impl: str
    sections: tuple[Section, ...]
    #: what "changed the board" means for the finally: the swaps this plan makes
    notes: tuple[str, ...] = ()

    def checks(self) -> list[Check]:
        return [c for s in self.sections for c in s.checks]


def E(path: str, op: str, value: Any = None, said: str = "", **kw: Any) -> Expect:
    return Expect(path, op, value, said, **kw)


def _identity(static: str, impl: str) -> tuple[Expect, ...]:
    return (E("identity.shell_id", "eq", static, f"identity.shell_id {static}", stop=True),
            E("identity.harness_impl", "eq", impl, f"harness_impl {impl}", stop=True))


def _swap_identity(static: str, rm: str) -> tuple[Expect, ...]:
    return (E("identity.shell_id", "eq", static, f"shell {static}", stop=True),
            E("identity.rm_id", "eq", rm, f"identity.rm_id {rm}", stop=True),
            E("health.reachable", "true", said="reachable true"))


def _program(rm: str, rm_id: str, transport_said: str) -> tuple[Expect, ...]:
    return (E("result.verified", "true", said="verified"),
            E("result.rm_id", "eq", rm_id, f"programmed {rm} ({rm_id})"),
            E("result.transport", "prefix", "tcp", transport_said),
            E("result.card", "none", said="a swap only: not kept on the card"))


def _restore() -> tuple[Expect, ...]:
    return (E("result.verified", "true", said="verified"),
            E("result.rm_id", "eq", GREYBOX_RM, f"restored to the baseline ({GREYBOX_RM})"))


def _mcc_temp(check_id: str, section: str, title: str, evidence: str) -> Check:
    return Check(
        check_id, section, title, SAFE, ("mcc", "{B}", "temp"),
        (E("readings[name=mcc_temp].available", "true", said="mcc_temp available"),
         E("readings[name=mcc_temp].source", "eq", "mcc-console (hub)",
           "source mcc-console (hub)"),
         E("readings[name=mcc_temp].value", "range", (15.0, 75.0), "about 35 degC")),
        writes="", evidence=evidence, mcc=True, timeout_s=120.0,
        hint="nothing was sent. Another reader on tty_00 is reported as skipped (tty_00 "
             "busy), never retried; anything else: HIL failure table, R4/D4 rows")


# --- A5/A6: net-protocol v0.16 identity and locate (lane HIL-IDLOC) ----------------------------

#: ``board identity``'s verdicts (``services.board_identity.STATUS_*``)
IDENTITY_STATUSES = ("ok", "unset", "differs", "clash", "unknown")
#: where a v0.16 field came from: the board's own setting (/persist), the stage0 bake, the
#: image default (``identity.source.<field>`` on the wire)
IDENTITY_SOURCES = ("override", "stage0", "default")
#: A6's blink (1-30 s) and the refusal of an image without ``locate`` (R7b's words)
LOCATE_S = 5
NO_LOCATE = "harness feature 'locate'"


def identity_and_locate() -> tuple[Check, ...]:
    """A5 and A6 (HIL_LINUX.md §A). Both pass on an image with the v0.16 ``identity`` and
    ``locate`` features (rc2_v7n) AND on one without them (v6n): A5 records what the board
    says (a label that is not its hub's is a finding, never a failure); A6 blinks, or is
    refused with exit 12 naming the feature. Anything else fails."""
    return (
        Check("A5", "A", "Board identity (net-protocol v0.16)", READ,
              ("board", "identity", "{B}"),
              (E("identity.status", "in", IDENTITY_STATUSES,
                 "a verdict (ok, unset, differs, clash or unknown): recorded, a label "
                 "mismatch is not a failure"),
               E("identity.reported.source.label", "in", IDENTITY_SOURCES,
                 "the label's source (override = /persist, stage0 = the bake, default) when "
                 "the image has the identity verb", optional=True),
               E("identity.reported.label", "regex", r"^[A-Za-z0-9-]{1,32}$",
                 "a label (MPS3 on v7n with no bake) when the board reports one",
                 optional=True)),
              evidence="a5_identity",
              record={"id_status": "identity.status", "id_label": "identity.reported.label",
                      "id_source": "identity.reported.source.label",
                      "id_via": "identity.reported.via", "id_verb": "identity.reported.feature"},
              note="{identity.status}: label {identity.reported.label} (source "
                   "{identity.reported.source.label}), hostname {identity.reported.hostname}, "
                   "ip {identity.reported.ip}, mac {identity.reported.mac}, via "
                   "{identity.reported.via}; image {fact:harness_version}, features "
                   "{fact:features}",
              hint="exit 12: the pack has no identity service (update Harness Manager). "
                   "A label, IP or MAC unlike the hub's (v6n reports board 1's MPS3-01, "
                   "192.168.10.101, 02:00:00:4d:50:53 on every board) is recorded, not failed"),
        Check("A6", "A", "Locate: blink the panel", SAFE,
              ("identify", "{B}", "--seconds", str(LOCATE_S)),
              (E("seconds", "eq", LOCATE_S, f"blinks {LOCATE_S} s"),
               E("until_ms", "range", (1, 30_000), "the board's own countdown (until_ms)")),
              answers=(Answer(12, (
                  E("error.name", "eq", "UNAVAILABLE", "exit 12, unavailable"),
                  E("error.message", "contains", NO_LOCATE,
                    f"Identify isn't available on this harness image ({NO_LOCATE})")),
                  note=f"not on this image: refused, exit 12 ({NO_LOCATE})"),),
              evidence="a6_locate",
              note=f"blinked {LOCATE_S} s: IDENTIFY: <this Harness Manager's user@host> on the "
                   "panel",
              writes=f"none persistent: a {LOCATE_S} s backlight blink and an IDENTIFY banner "
                     "(no claim lock; an image without 'locate' refuses)",
              hint="exit 12 with another reason, or any other exit: a Harness Manager or "
                   "harness bug (A1 says which image); exit 8 ALREADY: another Identify on "
                   "this board in the last 10 s"),
    )


# --- docs/HIL_LINUX.md ----------------------------------------------------------------------------


#: The Linux plans: ``card`` (the card usable), ``netboot`` (a blank card, both slot headers
#: zeroed: HIL_LINUX.md "Netboot mode"), ``nocard`` (no user microSD at all: "Card-less mode")
LINUX_MODES = {"linux": "card", "linux-netboot": "netboot", "linux-nocard": "nocard"}
NO_CARD = "no user microSD"


def linux(*, mode: str = "card", static: str = LINUX_STATIC) -> Plan:
    if mode not in LINUX_MODES.values():
        raise ValueError(f"unknown Linux plan mode {mode!r}")
    netboot = mode in ("netboot", "nocard")         # nocard: it can only netboot
    nocard = mode == "nocard"
    nb = "netboot mode: " if netboot else ""
    if nocard:
        card_skip = f"{NO_CARD} (HIL_LINUX.md, Card-less mode 1)"
        g_skip = (f"{NO_CARD} (Card-less mode 1): §G writes the config SD and ends in an MCC "
                  "REBOOT, and a card-less board gets no reset of any kind")
    else:
        card_skip = (nb + "the user microSD is unusable (HIL_LINUX.md, Netboot mode 1)") \
            if netboot else ""
        g_skip = (nb + "§G reads and writes the config SD A/B (Netboot mode 1)") if netboot else ""
    if nocard:
        d4_skip = (f"{NO_CARD} (Card-less mode 1): nothing to boot from the card, and no MCC "
                   "REBOOT on a card-less board (a failed cold boot needs a person at PB0)")
    elif netboot:
        d4_skip = nb + "skip D4 while the soak names tty_00 (Netboot mode 4)"
    else:
        d4_skip = ""
    f6_skip = (f"{NO_CARD} (Card-less mode 1): F6 writes the config SD, then REBOOTs: no reset "
               "of any kind on a card-less board") if nocard else ""
    s0 = Section("0", "Setup", (
        Check("0.1", "0", "The evidence folder and the environment", MANUAL,
              why="david's setup before the run: env.sh (overlay dirs, hw_server), boards.toml, "
                  "SSH-OK to the hub. The runner reads what it gives"),
        Check("0.2", "0", "Nothing reads the MCC console: no share on tty_00", READ,
              ("share", "list", "{B}"),
              (E("shares", "no_item_endswith", ("tty", "/tty_00"),
                 "share list shows no /dev/<hub target>/tty_00"),),
              evidence="0_shares_before",
              hint="someone else's share holds the MCC: every REBOOT refuses. Ask the Linux "
                   "lead; never `share stop` (it stops every share). The hub-side `pgrep` half "
                   "of 0.2 is manual"),
        Check("0.3", "0", "The lease is held here", READ, ("lease", "show", "{B}"),
              (E("lease.here", "true", said="held … — yours (this Harness Manager holds the "
                                           "token)"),
               E("lease.mine", "true", said="yours")),
              evidence="0_lease_show",
              hint="david takes it before the run (`harness-manager lease acquire $B --ttl … "
                   "--holder david-hm`); the runner never acquires one"),
        Check("0.4", "0", "The Harness Manager version", READ, ("version",),
              (E("version", "present", said="the version"),), evidence="0_hm_version"),
    ))
    sA = Section("A", "The Linux harness through Harness Manager", (
        Check("A1", "A", "Identity", READ, ("info", "{B}"),
              (*_identity(static, "linux"),
               E("identity.ver32", "eq", "0x01000000", "ver32 0x01000000 when reported",
                 optional=True),
               E("identity.usercode", "eq", "0xfb1f8c76", "usercode 0xfb1f8c76 when reported",
                 optional=True),
               E("identity.features", "has", "usd", "features lists usd"),
               E("health.reachable", "true", said="health.reachable true"),
               E("claim", "present", said="a claim block")),
              evidence="a1_info",
              record={"rm_id": "identity.rm_id", "features": "identity.features",
                      "harness_version": "identity.harness_version"},
              hint="`harness_impl` not linux or shell_id not the static: the board is not on "
                   "RC2 (rolled back?): STOP, ask the Linux lead. `offline`: wait 60 s, "
                   "repeat; then ask the Linux lead"),
        Check("A2", "A", "Front panel", READ, ("panel", "show", "{B}"),
              (E("panel.source", "in", ("panel", "rebuilt"), "source panel, else rebuilt"),
               E("panel.touch", "present", said="a touch line")),
              evidence="a2_panel"),
        Check("A3", "A", "XVC status", READ, ("xvc", "status", "{B}"),
              (E("state", "eq", "down", "state down"),
               E("scope", "contains", "never whole-device JTAG", "the scope line"),
               E("reach", "in", ("board-ssh", "hub-tunnel"),
                 "reach board-ssh (claim held here) or hub-tunnel (before the adopt)")),
              evidence="a3_xvc"),
        Check("A4", "A", "The finger test", MANUAL,
              why="a person holds a finger on the panel for 10 s"),
        *identity_and_locate(),
    ))
    sB = Section("B", "The SSH claim: check it, adopt it, never re-claim", (
        Check("B1", "B", "Who claimed the board?", READ, ("board", "claim-status", "{B}"),
              (E("claim.state", "in", ("other", "mine"),
                 "claimed by another key (or by you, once adopted)"),
               E("claim.host_key.match", "ne", False, "no HOST KEY CHANGED")),
              evidence="b1_claim_status", record={"claim": "claim.state"},
              hint="`unclaimed`: never claim; ask the Linux lead (a claim now would lock their "
                   "keys out). HOST KEY CHANGED: see the failure table (B2 rows)"),
        Check("B2", "B", "Adopt the claim", MANUAL,
              why="a trust decision: it pins the board's host key in boards.toml and asks `y`"),
        Check("B3", "B", "The pinned SSH works", READ,
              ("board", "ssh", "{B}", "-c", "cat /run/mps3/persist.state"),
              (E("$stdout", "regex", r"backing=tmpfs" if netboot
                 else r"backing=card dev=/dev/mmcblk0p3 storage=ok",
                 "persist.state names tmpfs (netboot)" if netboot
                 else "backing=card dev=/dev/mmcblk0p3 storage=ok"),),
              text=True, evidence="b3_ssh", needs=("claim", "mine"),
              needs_why="needs B2's adopt (B1 did not say `claimed by you`)",
              hint="the key does not log in: redo B2; check `ssh mps3-b2 true`"),
    ))
    c1_ok: tuple[int, ...] = (0,)
    if nocard:
        # harnessd answers `slot status` with card:false and no slots (slot_linux.c
        # mps3_slot_op); Harness Manager's slot service reports that as UNAVAILABLE (exit 12)
        # with os_slots.slots_reason's words, so the JSON has no `card` field to read.
        c1_ok = (12,)
        c1 = (E("error.name", "eq", "UNAVAILABLE", "exit 12, unavailable"),
              E("error.reason", "prefix", "no user microSD card in the slot",
                "no user microSD card in the slot (the harness's card:false): not a fault"))
    elif netboot:
        c1 = (E("slots.A.state", "eq", "empty", "slot A empty (no S0LB header): the zeroed "
                                                "headers, not a fault"),
              E("slots.B.state", "eq", "empty", "slot B empty (no S0LB header)"),
              E("job.busy", "false", said="no job running"))
    else:
        c1 = (E("running", "eq", "A", "running A"), E("default", "eq", "A", "default A"),
              E("slots.A.state", "eq", "valid", "slot A valid"),
              E("slots.B.state", "eq", "valid", "slot B valid"),
              E("job.busy", "false", said="a job line with no job running"))
    sC = Section("C", "OS slots and the user microSD (read)", (
        Check("C1", "C", "The OS slots" + (" (the card-less check)" if nocard
                                           else " (the netboot check)" if netboot else ""),
              READ, ("slot", "status", "{B}"), c1, exit_ok=c1_ok, evidence="c1_slot_status",
              hint=("exit 0 with slots: a card is in the slot, so this is not a card-less board "
                    "(use --plan linux-netboot, or linux). Another exit-12 reason (rescue, the "
                    "harness did not answer): HIL_LINUX.md's failure table, A1 rows"
                    if nocard else "")),
        Check("C2", "C", "The card", READ, ("card", "status", "{B}"),
              (E("present", "true", said="card <board>: valid"),
               E("state", "eq", "valid", "valid")),
              evidence="c2_card_status", skip=card_skip, record={"card_line": "line"}),
    ))
    sD = Section("D", "Keep on the card, then an MCC REBOOT", (
        Check("D1", "D", "What loads", READ, ("overlays", "{B}"),
              (E("compatible", "any_item", {"name": "nanosoc"}, "nanosoc on an ok line"),
               E("compatible", "any_item", {"name": "nanosoc_ila"}, "nanosoc_ila on an ok line"),
               E("incompatible", "no_item_in", ("name", ("greybox", "nanosoc", "nanosoc_ila")),
                 "nothing that should load under cannot")),
              evidence="d1_overlays",
              hint="the overlays folder is gone or keyed to another static: "
                   "HARNESS_MANAGER_MPS3_OVERLAY_DIRS (0.1)"),
        Check("D2", "D", "Program nanosoc and keep it on the card", MANUAL,
              why="WRITES the user microSD (keep on the card)", skip=card_skip),
        Check("D3", "D", "The card took it", MANUAL, why="follows D2", skip=card_skip),
        _mcc_temp("D4a", "D", "The MCC read on the hub (the one-reader scan D4's REBOOT needs)",
                  "d4a_mcc_temp"),
        Check("D4", "D", "REBOOT the board", MANUAL,
              why="an MCC REBOOT is never unattended (it power-cycles the board)", skip=d4_skip),
        Check("D5", "D", "The board came back running nanosoc from the card (the DAP says so)",
              MANUAL,
              why="follows D4; the pass is D4's `design verified` (the DAP answers 0x6ba00477), "
                  "never the reported rm_id. Linux v2.0.0: KNOWN ISSUE 12, power-on "
                  "failed:timeout with the greybox resident (FIX-PACK-6)", skip=card_skip),
    ))
    sE = Section("E", "XVC W5 on nanosoc_ila", (
        Check("E1", "E", "Load the ILA design (a swap; not kept on the card)", SAFE,
              ("program", "{B}", "nanosoc_ila", "--yes"),
              _program("nanosoc_ila", "0x0100000a", "via tcp; verified"),
              writes="partition swap to nanosoc_ila (Z2 swaps back)", evidence="e1_program_ila",
              timeout_s=600.0,
              hint="refused because the power-on load is running: wait for `power-on loaded`; "
                   "a card job running: the reset guard (never --force)"),
        Check("E1b", "E", "The board runs nanosoc_ila", READ, ("info", "{B}"),
              _swap_identity(static, "0x0100000a"), evidence="e1b_info",
              needs=("swapped", "nanosoc_ila"), needs_why="needs E1 (--writes safe)",
              record={"rm_id": "identity.rm_id"}),
        Check("E2", "E", "Open XVC", MANUAL,
              why="the XVC session is for E4's Vivado; it needs B2's adopt"),
        Check("E3", "E", "The Tcl and the hw_server", MANUAL, why="follows E2"),
        Check("E4", "E", "Vivado 2026.1", MANUAL,
              why="a person reads get_hw_targets/get_hw_ilas in Vivado"),
        Check("E5", "E", "Close XVC", MANUAL, why="follows E2"),
    ))
    # §F is never unattended, in every plan: raw ssh/fpgahub on the hub (no HM verb, so the
    # allow-list could not send it anyway), and F3 must stage THIS board's own bake (a table).
    sF = Section("F", "The hub SD door", tuple(
        Check(cid, "F", title, MANUAL, why=why, skip=f6_skip if cid == "F6" else "")
        for cid, title, why in (
            ("F1", "How the daemon is sandboxed", "raw ssh on the hub, not an HM verb"),
            ("F2", "The hub offers an sd method", "raw fpgahub on the hub, not an HM verb"),
            ("F3", "Stage the board's own base image",
             "writes the hub user's cache over raw ssh, from the board's row of F3's table"),
            ("F4", "The read probe", "raw fpgahub on the hub, not an HM verb"),
            ("F6", "The door, for real (opt-in)", "WRITES THE CONFIG SD, then REBOOTs"),
        )))
    sG = Section("G", "Config SD A/B by pointer", tuple(
        Check(cid, "G", title, MANUAL, why=why, skip=g_skip) for cid, title, why in (
            ("G1", "On the hub: read the card", "mounts the config SD with sudo on the hub"),
            ("G2", "On the hub: copy and point", "WRITES the config SD with sudo on the hub"),
            ("G3", "REBOOT", "an MCC REBOOT is never unattended"),
            ("G4", "The pointer back", "WRITES the config SD with sudo on the hub"),
        )))
    sZ = Section("Z", "Close-out", (
        Check("Z1", "Z", "The card's power-on default back", MANUAL,
              why="WRITES the user microSD (card clear / keep on the card)", skip=card_skip),
        Check("Z2", "Z", "Greybox", SAFE, ("restore", "{B}"), _restore(),
              writes="partition swap back to greybox", evidence="z2_restore", timeout_s=600.0,
              needs=("changed", True), needs_why="nothing was swapped (--writes none)",
              hint="refused by the reset guard (a card job): the runner waits, then reports; "
                   "never --force"),
        Check("Z2b", "Z", "The board is on greybox", READ, ("info", "{B}"),
              _swap_identity(static, GREYBOX_RM), evidence="z2b_info",
              needs=("changed", True), needs_why="nothing was swapped (--writes none)",
              record={"rm_id": "identity.rm_id"}),
        Check("Z2c", "Z", "The card's default is C2's", READ, ("card", "status", "{B}"),
              (E("line", "eq_fact", "card_line", "the card's default line equals C2's"),),
              evidence="z3_card_final", skip=card_skip),
        Check("Z3", "Z", "Close the board; no tunnel left", MANUAL,
              why="david closes the app; `pgrep -af -- '-N -T'` on srv03335"),
        Check("Z4", "Z", "Release the lease", MANUAL,
              why="the runner never releases the lease: david does, after reading REPORT.md"),
        Check("Z5", "Z", "Tell the Linux lead and the guide session", MANUAL,
              why="a message from david"),
        Check("Z6", "Z", "Send the evidence", MANUAL,
              why="the runner writes summary.json and REPORT.md; david sends the folder"),
    ))
    name = next(n for n, m in LINUX_MODES.items() if m == mode)
    return Plan(name, "docs/HIL_LINUX.md", static, "linux",
                (s0, sA, sB, sC, sD, sE, sF, sG, sZ))


# --- docs/HIL_B0.md -------------------------------------------------------------------------------


def bare_metal(*, static: str = BARE_METAL_STATIC) -> Plan:
    s0 = Section("0", "Once, before the window", (
        Check("0", "0", "Evidence folder, boards.toml, OpenOCD, SSH-OK", MANUAL,
              why="david's setup before the run"),))
    s1 = Section("1", "Take the lease", (
        Check("1", "1", "The lease is held here", READ, ("lease", "show", "{B}"),
              (E("lease.here", "true", said="held by david-hm — yours (the token is here)"),),
              evidence="t1_lease_show",
              hint="david takes it before the run; the runner never acquires one"),))
    s2 = Section("2", "Check nothing reads the MCC console (tty_00)", (
        Check("2", "2", "No share on tty_00", READ, ("share", "list", "{B}"),
              (E("shares", "no_item_endswith", ("tty", "/tty_00"), "no share on tty_00"),),
              evidence="t2_shares",
              hint="another reader on tty_00: the MCC checks refuse, naming it. A share goes "
                   "only with `share stop` (every share): david's call"),))
    s3 = Section("3", "Start the app", (
        Check("3", "3", "The app and the tunnel", MANUAL,
              why="the Overview and the tunnel's pgrep are looked at by a person"),))
    s4 = Section("4", "Read-only checks", (
        Check("R1", "4", "Identity and health", READ, ("info", "{B}"),
              (*_identity(static, "bare-metal"),
               E("identity.harness_version", "eq", "1.0.0", "harness_version 1.0.0"),
               E("health.reachable", "true", said="health.reachable true"),
               E("capabilities", "has", "console_dut", "capabilities include console_dut"),
               E("capabilities", "has", "console_controller", "… console_controller"),
               E("capabilities", "has", "telemetry_temp", "… telemetry_temp")),
              evidence="r1_info", record={"rm_id": "identity.rm_id"},
              hint="`offline`: the hub reached no shell (board off, rebooting, harness down); "
                   "`busy`: a second client on 6900"),
        Check("R2", "4", "A console in the GUI", MANUAL, why="a screenshot of the app"),
        Check("R3", "4", "The same console in screen", MANUAL, why="an interactive `screen`"),
        _mcc_temp("R4", "4", "MCC temperature, read on the hub", "r4_mcc_temp"),
        Check("R5", "4", "Debug detect (on greybox)", READ, ("debug", "detect", "{B}"),
              (E("error.message", "contains", "has no debug port",
                 "on greybox: exit 13, the loaded design (greybox) has no debug port"),),
              exit_ok=(13,), evidence="r5_debug_detect",
              hint="exit 12 naming OpenOCD: set HARNESS_MANAGER_OPENOCD (HIL_B0.md 0.3) in "
                   "the runner's environment"),
        Check("R6", "4", "The lease", READ, ("lease", "show", "{B}"),
              (E("lease.mine", "true", said="mine true"),
               E("lease.here", "true", said="held here"),
               E("lease.holder_kind", "eq", "hm", "holder_kind hm"),
               E("notes_supported", "true", said="notes_supported true (SSH hub)"),
               E("can_revoke", "true", said="can_revoke true (SSH hub)")),
              evidence="r6_lease"),
        Check("R7", "4", "Front panel on bare metal", READ, ("panel", "show", "{B}"),
              (E("panel.source", "eq", "rebuilt", "source rebuilt from what Harness Manager "
                                                  "read"),
               E("panel.owner", "present", said="the owner (harness or DUT)")),
              evidence="r7_panel"),
        Check("R7b", "4", "Identify on bare metal (refused: no blink)", SAFE,
              ("identify", "{B}"),
              (E("error.message", "contains", "harness feature 'locate'",
                 "unavailable — Identify isn't available on this harness image yet (harness feature 'locate'), rc=12"),),
              exit_ok=(12,), evidence="r7b_identify",
              writes="none expected (bare metal refuses; a harness with 'locate' blinks the "
                     "backlight)"),
        Check("R8", "4", "XDC for the fielded static", MANUAL,
              why="the model card and the preview in the app (a screenshot)"),
        Check("R9", "4", "Build guide against the board", MANUAL, why="the app's Build section"),
        Check("R10", "4", "XVC status (read-only)", READ, ("xvc", "status", "{B}"),
              (E("state", "eq", "down", "down"),
               E("scope", "contains", "never whole-device JTAG", "the scope line"),
               E("warnings", "any_contains", "unauthenticated",
                 "the bare-metal warning (unauthenticated until cutover)")),
              evidence="r10_xvc"),
        Check("R11", "4", "Harness versions", READ, ("harness", "list", "{B}"),
              (E("error.name", "ne", "FAILED", "a clear message, not a traceback"),),
              exit_ok=(0, 3, 7, 12), evidence="r11_harness",
              hint="exit 1 is an internal error (a traceback): report it"),
    ))
    s5 = Section("5", "Write checks", (
        Check("W1", "5", "Program nanosoc", SAFE, ("program", "{B}", "nanosoc", "--yes"),
              _program("nanosoc", "0x01000001", "via tcp+windowed; verified"),
              writes="partition swap to nanosoc (W4 swaps back)", evidence="w1_program",
              timeout_s=600.0),
        Check("W1b", "5", "The board runs nanosoc", READ, ("info", "{B}"),
              _swap_identity(static, "0x01000001"), evidence="w1b_info",
              needs=("swapped", "nanosoc"), needs_why="needs W1 (--writes safe)",
              record={"rm_id": "identity.rm_id"}),
        Check("W2", "5", "A console", MANUAL, why="`screen` and Reset DUT in the GUI"),
        Check("W3", "5", "Debug up + gdb", MANUAL, why="holds a server; gdb by hand"),
        Check("W5", "5", "XVC session", MANUAL, why="Vivado 2024.1 by hand"),
        Check("W4", "5", "Restore greybox", SAFE, ("restore", "{B}"), _restore(),
              writes="partition swap back to greybox", evidence="w4_restore", timeout_s=600.0,
              needs=("changed", True), needs_why="nothing was swapped (--writes none)"),
        Check("W4b", "5", "The board is on greybox", READ, ("info", "{B}"),
              _swap_identity(static, GREYBOX_RM), evidence="w4b_info",
              needs=("changed", True), needs_why="nothing was swapped (--writes none)",
              record={"rm_id": "identity.rm_id"}),
    ))
    s6 = Section("6", "Harness Manager on the Linux harness (end of B1 v4)", tuple(
        Check(cid, "6", title, MANUAL, skip="the Linux harness: use --plan linux") for cid, title in (
            ("S3.1", "Identity"), ("S3.2", "Front panel"), ("S3.3", "XVC status"),
            ("S3.4", "CLCD finger test"))))
    s7 = Section("7", "Close-out", (
        Check("7", "7", "Close the app, release the lease", MANUAL,
              why="the runner never releases the lease: david does, after reading REPORT.md"),))
    return Plan("bare-metal", "docs/HIL_B0.md", static, "bare-metal",
                (s0, s1, s2, s3, s4, s5, s6, s7))


PLANS: dict[str, Callable[..., Plan]] = {
    "linux-netboot": lambda **kw: linux(mode="netboot", **kw),
    "linux-nocard": lambda **kw: linux(mode="nocard", **kw),
    "linux": lambda **kw: linux(mode="card", **kw),
    "bare-metal": bare_metal,
}


def build(name: str, *, static: str | None = None) -> Plan:
    kw = {"static": static.lower()} if static else {}
    return PLANS[name](**kw)


# --- which plan fits a board (HIL-GUI) ------------------------------------------------------------


def _slot_states(card: dict[str, Any]) -> list[str]:
    os_slots = card.get("os_slots") if isinstance(card.get("os_slots"), dict) else {}
    slots = os_slots.get("slots") if isinstance(os_slots.get("slots"), dict) else {}
    return [str((v or {}).get("state", "")) for v in slots.values() if isinstance(v, dict)]


def auto_plan(identity: dict[str, Any] | None, card: dict[str, Any] | None) -> tuple[str, str]:
    """The plan that fits a board, and why (``docs/HIL_AUTO.md`` "In the app"): from its
    identity (``harness_impl``) and its user microSD (``GET /boards/{bid}/card``'s ``card``, or
    None when it was not read). The app's Checks section applies the same rule
    (``web/static/js/sections/checks.js`` ``autoPlan``; a test holds the two to one table):

    - not the Linux harness -> ``bare-metal`` (HIL_B0.md);
    - Linux, no card in the user microSD slot -> ``linux-nocard`` (board 2, Card-less mode);
    - Linux, a card with an OS slot that is valid -> ``linux`` (the card usable);
    - Linux, a card with no valid OS slot (both headers zeroed) -> ``linux-netboot``;
    - Linux, the card not read -> ``linux-netboot`` (the lab boards' state since 09-25), said.
    """
    impl = str((identity or {}).get("harness_impl") or "")
    if impl != "linux":
        return "bare-metal", (f"the {impl} harness" if impl else "not the Linux harness") \
            + " (HIL_B0.md)"
    if not isinstance(card, dict):
        return "linux-netboot", ("the Linux harness; its user microSD was not read, so a blank "
                                 "card (netboot) is assumed")
    if not card.get("present"):
        return "linux-nocard", "the Linux harness with no user microSD card (Card-less mode)"
    states = _slot_states(card)
    if any(s == "valid" for s in states) or (not states and card.get("state") == "valid"):
        return "linux", "the Linux harness with a usable user microSD card (an OS slot is valid)"
    return "linux-netboot", ("the Linux harness with a blank user microSD card (no valid OS "
                             "slot: Netboot mode)")
