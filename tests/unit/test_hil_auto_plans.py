"""HIL-AUTO: the runbooks and the plans cannot drift apart, and the allow-list is exact.

The runbooks (``docs/HIL_LINUX.md``, ``docs/HIL_B0.md``) are parsed here: every check id with
an **Expect** (a table row, for HIL_B0) must have a plan entry, every plan entry must name a
check the runbook has, and the Linux runbook's "Netboot mode" and "Card-less mode" skip lists
must be exactly what the netboot and nocard plans skip. Each rule has its negative twin (a
doctored runbook fails it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.hil import plans as P
from tools.hil.run import READ_ARGV, SAFE_ARGV, allowed

ROOT = Path(__file__).resolve().parents[2]
LINUX_MD = ROOT / "docs" / "HIL_LINUX.md"
B0_MD = ROOT / "docs" / "HIL_B0.md"
B = "192.168.10.101"

_HEAD = re.compile(r"^(?:\*\*(?P<bold>[A-Z]\d+(?: \+ [A-Z]\d+)*)[.\s(]|### (?P<num>\d\.\d) )")
_ROW = re.compile(r"^\| (?P<id>[A-Z]\d+(?:\.\d+)?) \|")
_SECTION = re.compile(r"^## (?P<id>[0-9A-Z])\. ")


def linux_checks(text: str) -> tuple[dict[str, bool], set[str]]:
    """{check id: has an **Expect**} and the section ids, from HIL_LINUX.md's headings."""
    ids: dict[str, bool] = {}
    sections: set[str] = set()
    current: list[str] = []
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m or line.startswith("## "):
            current = []
            if m:
                sections.add(m.group("id"))
            continue
        m = _HEAD.match(line)
        if m:
            current = [x.strip() for x in (m.group("bold") or m.group("num")).split("+")]
            for cid in current:
                ids.setdefault(cid, False)
        if current and "**Expect" in line:
            for cid in current:
                ids[cid] = True
    return ids, sections


def b0_checks(text: str) -> tuple[dict[str, bool], set[str]]:
    """{table-row id: True} (every row has an Expected column) and the section numbers."""
    ids = {m.group("id"): True for m in map(_ROW.match, text.splitlines()) if m}
    sections = {m.group("id") for m in map(_SECTION.match, text.splitlines()) if m}
    return ids, sections


def preface_skips(text: str, heading: str) -> set[str]:
    """The check ids a mode preface's item 1 skips: its bullet lines (``§G``: every G check;
    ``Z2's `card status``` : the Z2 follow-on that runs ``card status``)."""
    block = text.split(f"## {heading}", 1)[1].split("\n2. ", 1)[0]
    block = block.split("Never run", 1)[0]
    block = "\n".join(line for line in block.splitlines() if line.lstrip().startswith("- "))
    out: set[str] = set()
    plan = P.build("linux")
    for m in re.finditer(r"§?(?P<id>[A-Z]\d?)(?P<part>'s `card status`)?", block):
        cid, part = m.group("id"), m.group("part")
        if len(cid) == 1:
            if m.group(0).startswith("§"):
                out |= {c.id for c in plan.checks() if c.section == cid}
            continue
        if part:
            out |= {c.id for c in plan.checks()
                    if base(c.id) == cid and c.argv[:2] == ("card", "status")}
        else:
            out.add(cid)
    return out


def netboot_skips(text: str) -> set[str]:
    return preface_skips(text, "Netboot mode")


def cardless_skips(text: str) -> set[str]:
    return preface_skips(text, "Card-less mode")


def base(cid: str) -> str:
    """``R7b`` -> ``R7``: a lettered follow-on is its runbook check's second command."""
    return re.sub(r"(?<=\d)[a-z]$", "", cid)


def drift(runbook_ids: dict[str, bool], sections: set[str], plan: P.Plan) -> list[str]:
    plan_ids = {c.id for c in plan.checks()}
    plan_base = {base(i) for i in plan_ids}
    out = [f"{cid} has an Expect in {plan.runbook} but no entry in plan {plan.name}"
           for cid, expect in sorted(runbook_ids.items()) if expect and cid not in plan_base]
    out += [f"plan {plan.name} has {cid}, which {plan.runbook} does not"
            for cid in sorted(plan_ids) if base(cid) not in runbook_ids and cid not in sections]
    out += [f"plan {plan.name} section {s.id} is not a section of {plan.runbook}"
            for s in plan.sections if s.id not in sections]
    out += [f"{c.id} sits in §{c.section}, which is not its section in the plan"
            for s in plan.sections for c in s.checks if c.section != s.id]
    return out


# --- the runbooks and the plans -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["linux", "linux-netboot", "linux-nocard"])
def test_every_linux_runbook_check_has_a_plan_entry_and_vice_versa(name):
    ids, sections = linux_checks(LINUX_MD.read_text(encoding="utf-8"))
    assert {"A1", "B3", "C1", "D4", "E1", "G2", "Z2", "0.3"} <= set(ids)   # the parser works
    assert drift(ids, sections, P.build(name)) == []


def test_every_b0_runbook_row_has_a_plan_entry_and_vice_versa():
    ids, sections = b0_checks(B0_MD.read_text(encoding="utf-8"))
    assert {"R1", "R11", "W4", "S3.4"} <= set(ids)
    assert drift(ids, sections, P.build("bare-metal")) == []


def test_twin_a_new_runbook_check_with_an_expect_fails_the_drift_test():
    text = LINUX_MD.read_text(encoding="utf-8").replace(
        "**A4. The finger test**", "**A5. A new check**\n**Expect:** something.\n\n"
                                   "**A4. The finger test**")
    ids, sections = linux_checks(text)
    assert drift(ids, sections, P.build("linux-netboot")) == [
        "A5 has an Expect in docs/HIL_LINUX.md but no entry in plan linux-netboot"]


def test_twin_a_check_the_runbook_dropped_fails_the_drift_test():
    text = LINUX_MD.read_text(encoding="utf-8").replace("**B3. The pinned SSH works**",
                                                        "**The pinned SSH works**")
    ids, sections = linux_checks(text)
    assert "plan linux has B3, which docs/HIL_LINUX.md does not" in drift(
        ids, sections, P.build("linux"))


def test_twin_a_dropped_b0_row_fails_the_drift_test():
    text = B0_MD.read_text(encoding="utf-8").replace("| R10 |", "| X10 |")
    ids, sections = b0_checks(text)
    assert "plan bare-metal has R10, which docs/HIL_B0.md does not" in drift(
        ids, sections, P.build("bare-metal"))


def test_the_netboot_plan_skips_exactly_what_the_netboot_preface_lists():
    skips = netboot_skips(LINUX_MD.read_text(encoding="utf-8"))
    assert {"C2", "D2", "D3", "D5", "G1", "G2", "G3", "G4", "Z1", "Z2c"} <= skips
    plan = P.build("linux-netboot")
    card_skipped = {c.id for c in plan.checks() if c.skip and "D4" not in c.id}
    assert card_skipped == skips


def test_the_nocard_plan_skips_exactly_what_the_cardless_preface_lists():
    skips = cardless_skips(LINUX_MD.read_text(encoding="utf-8"))
    assert {"C2", "D2", "D3", "D4", "D5", "F6", "G1", "G2", "G3", "G4", "Z1", "Z2c"} == skips
    plan = P.build("linux-nocard")
    assert {c.id for c in plan.checks() if c.skip} == skips
    assert all(c.skip.startswith("no user microSD") for c in plan.checks() if c.skip)


def test_twin_a_skip_added_to_the_cardless_preface_fails_the_comparison():
    text = LINUX_MD.read_text(encoding="utf-8").replace(
        "   - §F6 and §G (the config SD, then an MCC REBOOT);",
        "   - §F6 and §G (the config SD, then an MCC REBOOT);\n   - §E1;")
    assert "E1" in cardless_skips(text)
    assert {c.id for c in P.build("linux-nocard").checks() if c.skip} != cardless_skips(text)


def test_twin_the_netboot_plan_does_not_match_the_cardless_preface():
    skips = cardless_skips(LINUX_MD.read_text(encoding="utf-8"))
    netboot = {c.id for c in P.build("linux-netboot").checks() if c.skip}
    assert netboot != skips and skips - netboot == {"F6"}


def test_the_nocard_c1_expects_the_no_card_answer():
    """harnessd says card:false; HM's CLI says so as exit 12 with the slot service's reason."""
    c1 = next(c for c in P.build("linux-nocard").checks() if c.id == "C1")
    got = {(e.path, e.op, e.value) for e in c1.expects}
    assert got == {("error.name", "eq", "UNAVAILABLE"),
                   ("error.reason", "prefix", "no user microSD card in the slot")}
    assert c1.tier == P.READ and c1.argv == ("slot", "status", "{B}") and c1.exit_ok == (12,)
    # twin: the netboot plan's C1 wants the two zeroed slots, which a card-less board lacks
    nb = next(c for c in P.build("linux-netboot").checks() if c.id == "C1")
    assert {e.path for e in nb.expects} >= {"slots.A.state", "slots.B.state"}


def test_the_nocard_plan_resets_nothing_and_keeps_the_swaps_and_the_mcc_read():
    plan = P.build("linux-nocard")
    runs = {c.id: c for c in plan.checks() if not c.skip and c.tier != P.MANUAL}
    assert {i for i, c in runs.items() if c.tier == P.SAFE} == {"D4a", "E1", "Z2"}
    assert [c.argv for c in runs.values() if c.tier == P.SAFE] == [
        ("mcc", "{B}", "temp"), ("program", "{B}", "nanosoc_ila", "--yes"), ("restore", "{B}")]
    reboots = {c.id for c in plan.checks()
               if c.tier == P.MANUAL and "REBOOT" in (c.why + c.title).upper()}
    assert reboots == {"D4", "F6", "G3"}
    assert all(next(c for c in plan.checks() if c.id == i).skip for i in reboots)


def test_section_f_is_never_unattended_in_any_plan():
    """§F runs raw ssh/fpgahub on the hub: manual in every Linux plan, no argv to send, and
    F3 says it stages the board's own bake (HIL_LINUX.md F3's per-board table)."""
    for name in ("linux", "linux-netboot", "linux-nocard"):
        f = [c for c in P.build(name).checks() if c.section == "F"]
        assert [c.id for c in f] == ["F1", "F2", "F3", "F4", "F6"], name
        assert all(c.tier == P.MANUAL and not c.argv for c in f), name
        f3 = next(c for c in f if c.id == "F3")
        assert "board's row" in f3.why and "own" in f3.title.lower()


def test_f3_names_each_boards_own_bake():
    text = LINUX_MD.read_text(encoding="utf-8")
    f3 = text.split("**F3. ", 1)[1].split("**F4. ", 1)[0]
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| (\d) \(`lab2?`\) \|.*$", f3, re.M)}
    assert set(rows) == {"1", "2"}
    assert "mps3_01_pl" in rows["1"] and "0xC457D656" in rows["1"] and "192.168.10.101" in rows["1"]
    assert rows["1"].count("286ae54d2a2b8c15e8b610df8088d37e5c3b3c706aa9206aceade2b503f081b4") == 1
    assert "/home/david/pv_rb/config_rm_greybox_stage0.bit" in rows["1"]
    assert "mps3_02_pl" in rows["2"] and "0x6FAE6A0B" in rows["2"] and "192.168.11.101" in rows["2"]
    assert rows["2"].count("f206f788f7497b650b6f0408ebb2fbdb795edb749784a3ec42e6caaaa3df5058") == 1
    assert "286ae54d" not in rows["2"] and "f206f788" not in rows["1"]
    # the command stages $BAKE only when its sha256 is the row's; nothing names one board's bit
    assert 'if [ "$SHA" = "$BAKE_SHA" ]; then' in f3 and "pv_rb" not in f3.split("```bash")[1]


def test_twin_the_full_linux_plan_skips_none_of_them():
    assert [c.id for c in P.build("linux").checks() if c.skip] == []


def test_twin_a_skip_added_to_the_preface_fails_the_netboot_comparison():
    text = LINUX_MD.read_text(encoding="utf-8").replace("   - §C2 (the card);",
                                                        "   - §C2 (the card);\n   - §B3;")
    skips = netboot_skips(text)
    plan = P.build("linux-netboot")
    assert {c.id for c in plan.checks() if c.skip and "D4" not in c.id} != skips


def test_every_check_is_well_formed():
    for name in P.PLANS:
        plan = P.build(name)
        ids = [c.id for c in plan.checks()]
        assert len(ids) == len(set(ids)), name
        for c in plan.checks():
            assert c.tier in (P.READ, P.SAFE, P.MANUAL)
            if c.tier == P.MANUAL:
                assert c.why or c.skip, c.id
                assert not c.argv
            else:
                assert c.argv and c.expects, c.id
                assert c.evidence, c.id


def test_every_automatic_argv_passes_its_own_tier_and_nothing_more():
    for name in P.PLANS:
        for c in P.build(name).checks():
            if c.tier == P.MANUAL:
                continue
            argv = [a.replace("{B}", B) for a in c.argv]
            assert allowed(argv, B, "safe"), c.id
            assert allowed(argv, B, "none") == (c.tier == P.READ), c.id


def test_expect_static_overrides_the_runbooks_static():
    plan = P.build("linux-netboot", static="0x1234ABCD")
    a1 = next(c for c in plan.checks() if c.id == "A1")
    assert plan.static == "0x1234abcd"
    assert any(e.value == "0x1234abcd" and e.stop for e in a1.expects)


# --- the allow-list ----------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["program", B, "nanosoc", "--yes", "--keep-on-card"],
    ["program", B, "nanosoc", "--keep-on-card", "--yes"],
    ["program", B, "--keep-on-card", "--yes"],
    ["program", B, "nanosoc"],                        # no --yes: it would prompt
    ["mcc", B, "reboot", "--yes"],
    ["mcc", B, "reboot", "--yes", "--force", "--consent", f"RESET {B}"],
    ["mcc", B, "cmd", "REBOOT"],
    ["slot", "push", B, "img"], ["slot", "commit", B], ["slot", "rollback", B, "--yes"],
    ["card", "clear", B], ["card", "commit", B],
    ["sd", B, "install", "bundle", "--backup", "z"], ["sd", B, "restore", "z"],
    ["share", "start", B, "mcc"], ["share", "start", B, "/dev/mps3_01_pl/tty_00"],
    ["lease", "acquire", B], ["lease", "release", B], ["lease", "force", B, "--yes"],
    ["lease", "request", B], ["board", "claim", B, "--adopt", "--yes"],
    ["board", "ssh", B, "-c", "reboot"], ["board", "ssh", B],
    ["harness", "install", B, "1.1.0"], ["power", "cycle", B], ["reset", B],
    ["info", "10.0.0.9"],                             # another board
])
def test_the_allow_list_refuses_every_write_in_both_modes(argv):
    assert not allowed(argv, B, "safe")
    assert not allowed(argv, B, "none")


def test_twin_the_swaps_and_the_mcc_read_pass_only_with_writes_safe():
    for argv in (["program", B, "nanosoc_ila", "--yes"], ["restore", B], ["mcc", B, "temp"]):
        assert allowed(argv, B, "safe")
        assert not allowed(argv, B, "none")
    assert all("{RM}" not in s for s in READ_ARGV)
    assert {s[0] for s in SAFE_ARGV} == {"program", "restore", "mcc", "identify"}
