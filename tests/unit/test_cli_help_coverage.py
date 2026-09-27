"""HELP-FIX: the CLI's help says what the CLI does, for every verb. Each check has a twin.

- every top-level verb has an entry in ``help --tabs`` (what the app's Help dialog shows);
- every option and positional has help text, and none carries a lane code (D11, T7's);
- a verb group's one-line summary names every subverb;
- ``--via`` has one metavar and one help everywhere, and the metavar is what
  ``tunnel.parse_via`` takes;
- ``--serial`` (the form that adds a link) carries its examples everywhere;
- console help: interactive, Ctrl-] exits, ``--read-only``; never "Ctrl-C stops";
- ``kit build --stop-after`` hides its empty (keep the script's) choice, and still takes it;
- ``slot push``: ``--bundle`` or ``--static-id`` is required, and both together still work;
- the unsigned-channel refusal names ``sd`` commands that parse.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest

from harness_manager.cli.context import SERIAL_HELP, VIA_HELP, VIA_METAVAR
from harness_manager.cli.helptext import render_tabs, tabs
from harness_manager.cli.main import main, make_parser
from harness_manager.core.errors import RefusedError, UsageError


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _verbs(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


def _summaries(parser: argparse.ArgumentParser) -> dict[str, str]:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {ca.dest: ca.help or "" for ca in sub._choices_actions}


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """(path, parser) for the parser and every sub-parser below it."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sp in action.choices.items():
                yield from _walk(sp, (*path, name))


# --- every top-level verb is in help --tabs --------------------------------------------------


def undocumented(text: str, verbs: list[str]) -> list[str]:
    """The verbs with no entry in the help tabs: an entry is a line that starts (after
    indentation and an optional ``harness-manager``) with the verb and a space."""
    have = set()
    for line in text.splitlines():
        m = re.match(r"^\s*(?:harness-manager\s+)?([a-z][a-z-]*)(?:\s|$)", line)
        if m:
            have.add(m.group(1))
    return [v for v in verbs if v not in have]


def test_every_top_level_verb_has_an_entry_in_help_tabs():
    verbs = sorted(_verbs(make_parser()))
    assert len(verbs) >= 38
    assert undocumented(render_tabs(tabs()), verbs) == []


def test_twin_a_verb_whose_entry_is_removed_is_reported():
    text = render_tabs(tabs())
    without = "\n".join(ln for ln in text.splitlines()
                        if not re.match(r"^\s*(harness-manager\s+)?identify\b", ln))
    assert "identify" in text and without != text
    assert undocumented(without, sorted(_verbs(make_parser()))) == ["identify"]
    # A verb named only in prose is not an entry either.
    assert undocumented("## Panel\nThe identify verb blinks the panel.\n", ["identify"]) \
        == ["identify"]


def test_twin_a_new_verb_with_no_entry_is_reported():
    assert undocumented(render_tabs(tabs()), ["program", "frobnicate"]) == ["frobnicate"]


def test_the_app_help_dialog_reads_the_same_tabs():
    """The daemon's GET /help/tabs (what the web Help dialog shows) is helptext.tabs()."""
    src = Path(__file__).resolve().parents[2] / "src/harness_manager/daemon/app.py"
    assert "helptext.tabs()" in src.read_text(encoding="utf-8")


# --- every argument has help; no lane codes ----------------------------------------------------


def missing_help(parser: argparse.ArgumentParser) -> list[str]:
    out = []
    for path, p in _walk(parser):
        for a in p._actions:
            if isinstance(a, (argparse._HelpAction, argparse._VersionAction,
                              argparse._SubParsersAction)):
                continue
            if a.help is None or not str(a.help).strip():
                out.append(f"{' '.join(path)} {'/'.join(a.option_strings) or a.dest}")
    return out


def test_every_option_and_positional_has_help():
    assert missing_help(make_parser()) == []


def test_twin_an_argument_without_help_is_reported():
    p = make_parser()
    _verbs(p)["probe"].add_argument("--frobnicate", metavar="N")
    assert missing_help(p) == ["probe --frobnicate"]


#: Internal lane and decision codes that mean nothing to a user.
JARGON = re.compile(r"\((?:[DTLHPUSQ]\d+|CCR[^)]*)\)|\b[DTLHPUQ]\d+'s\b|\bCCR\b|\blane\b",
                    re.IGNORECASE)


def _all_help_text() -> str:
    parts = [p.format_help() for _, p in _walk(make_parser())]
    return "\n".join(parts) + render_tabs(tabs())


def test_no_lane_or_decision_codes_in_user_help():
    # Flattened, so a code that argparse wrapped across two lines is still one match.
    # ("the FPGA UART lanes" is hardware, and \blane\b does not match "lanes".)
    assert JARGON.findall(" ".join(_all_help_text().split())) == []


def test_twin_the_jargon_pattern_catches_the_old_strings():
    for old in ("forget the last forced release of your lease (D11)",
                "restore this config-SD backup instead (T7's rollback)",
                "wire it in (CCR L1-2)"):
        assert JARGON.search(old), old
    assert not JARGON.search("the board's lease (the hub's lease_ttl, 1h)")


# --- group summaries name their subverbs --------------------------------------------------------

#: Verbs whose one-line summary is "what: a, b, c": each subverb must be in the list.
LISTED = ("lease", "hub", "harness", "update", "config", "daemon", "lab", "mcc", "sd", "slot",
          "card", "share")
#: A subverb the summary names by a group word.
GROUPED = {"config": {"set-secret": "secrets", "unset-secret": "secrets",
                      "clear-secret": "secrets"}}


def unnamed_subverbs(verb: str, summary: str, subverbs: list[str]) -> list[str]:
    words = set(re.findall(r"[a-z][a-z-]*", summary.lower()))
    alias = GROUPED.get(verb, {})
    return [s for s in subverbs if alias.get(s, s) not in words]


@pytest.mark.parametrize("verb", LISTED)
def test_a_group_summary_names_every_subverb(verb):
    p = make_parser()
    subverbs = list(_verbs(_verbs(p)[verb]))
    assert unnamed_subverbs(verb, _summaries(p)[verb], subverbs) == []


def test_the_summaries_the_docs_lane_found_short_now_name_their_subverbs():
    s = _summaries(make_parser())
    for verb, names in {"lease": ("requests", "respond", "dismiss"), "hub": ("token",),
                        "harness": ("unpin",), "config": ("unset", "path"),
                        "update": ("app", "status")}.items():
        assert all(re.search(rf"\b{n}\b", s[verb]) for n in names), (verb, s[verb])


def test_twin_a_summary_that_drops_a_subverb_is_reported():
    old = "the board's hub lease: show, acquire, request, force, leave, release"
    subverbs = list(_verbs(_verbs(make_parser())["lease"]))
    assert unnamed_subverbs("lease", old, subverbs) == ["requests", "respond", "dismiss"]


# --- --via and --serial --------------------------------------------------------------------------


def _options(flag: str) -> list[tuple[str, argparse.Action]]:
    return [(" ".join(path), a) for path, p in _walk(make_parser()) for a in p._actions
            if flag in a.option_strings]


def test_every_via_has_the_same_metavar_and_help():
    vias = _options("--via")
    where = {path.split()[0] for path, _ in vias if path}
    assert {"info", "program", "panel", "identify", "xvc", "slot", "card", "board",
            "probe"} <= where
    assert {(a.metavar, a.help) for _, a in vias} == {(VIA_METAVAR, VIA_HELP)}
    assert "hub" in VIA_METAVAR and "boards.toml via does the same" in VIA_HELP


def test_the_via_metavar_is_what_the_tunnel_accepts():
    from harness_manager_mps3.tunnel import parse_via

    assert VIA_METAVAR == "ssh:HOST|hub"
    assert parse_via("ssh:lab-hub") == "lab-hub" and parse_via("hub") == ""
    with pytest.raises(UsageError):               # twin: anything else is not a --via
        parse_via("lab-hub")


def test_every_serial_that_adds_a_link_carries_its_examples():
    adders = [(path, a) for path, a in _options("--serial") if a.default is argparse.SUPPRESS]
    assert {path.split()[0] for path, _ in adders} >= {"info", "update", "harness", "power",
                                                     "panel"}
    assert {a.help for _, a in adders} == {SERIAL_HELP}
    assert "serial:///dev/ttyUSB0" in SERIAL_HELP and "COM7" in SERIAL_HELP


# --- console, kit build, slot push, sd hint --------------------------------------------------------


def test_console_help_says_interactive_ctrl_bracket_and_read_only(capsys):
    tabs_text = render_tabs(tabs())
    assert "Ctrl-C stops" not in tabs_text                      # the old, wrong text
    consoles = dict(tabs())["Consoles"]
    assert "Ctrl-] exits" in consoles and "--read-only" in consoles
    assert "Ctrl-] exits" in dict(tabs())["Quick start"]
    _, out, _ = run(capsys, "help", "console")
    flat = " ".join(out.split())
    assert "--read-only" in flat and "Ctrl-]" in flat


def test_kit_build_hides_the_empty_stop_after_choice_and_still_takes_it():
    kit_build = _verbs(_verbs(make_parser())["kit"])["build"]
    assert "{," not in kit_build.format_help()
    assert "{preflight,synth,link,impl,verify,bitstream}" in kit_build.format_help()
    p = make_parser()
    assert p.parse_args(["kit", "build", "d"]).stop_after == ""
    assert p.parse_args(["kit", "build", "d", "--stop-after", ""]).stop_after == ""
    assert p.parse_args(["kit", "build", "d", "--stop-after", "synth"]).stop_after == "synth"
    with pytest.raises(UsageError):                                 # twin
        p.parse_args(["kit", "build", "d", "--stop-after", "route"])


def test_slot_push_help_says_bundle_or_static_id_is_required(capsys):
    _, out, _ = run(capsys, "help", "slot")
    push = _verbs(_verbs(make_parser())["slot"])["push"].format_help()
    flat = " ".join(push.split())
    assert "--bundle or --static-id is required" in flat
    assert "this or --static-id is required" in flat and "this or --bundle is required" in flat


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    from tests.fakes.s0lb_image import linux_bundle_s0lb, make_s0lb

    data = make_s0lb(b"\x5a" * 4096)
    (tmp_path / "linux_slot.img").write_bytes(data)
    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "targets": {"ethernet": {
               "slot_image": {"sha256": hashlib.sha256(data).hexdigest(),
                              "s0lb": linux_bundle_s0lb(data)},
               "provisioned": {"static_id": "0x72bb0a36"}}}}
    (tmp_path / "linux_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


def test_slot_push_takes_both_flags_when_they_agree(bundle):
    """Why there is no mutually-exclusive group: both flags together are a valid push today
    (the static-id must match the bundle's), so a group would refuse a working command."""
    from harness_manager.services.slots import push_source

    args = make_parser().parse_args(["slot", "push", "t", "--bundle", str(bundle),
                                     "--static-id", "0x72BB0A36"])
    assert args.bundle and args.static_id                         # argparse takes both
    src = push_source(None, bundle=Path(args.bundle), static_id=args.static_id)
    assert src.static_id == "0x72bb0a36"
    assert push_source(None, bundle=bundle).static_id == "0x72bb0a36"      # --bundle alone
    img = bundle / "linux_slot.img"
    assert push_source(img, static_id="0x72bb0a36").static_id == "0x72bb0a36"  # --static-id


def test_twin_slot_push_refuses_neither_and_a_contradiction(bundle):
    from harness_manager.services.slots import push_source

    with pytest.raises(UsageError, match="not known"):
        push_source(bundle / "linux_slot.img")
    with pytest.raises(UsageError, match="contradicts"):
        push_source(None, bundle=bundle, static_id="0x0EE58A4D")


def _backticked(text: str) -> list[str]:
    return re.findall(r"`harness-manager ([^`]+)`", text)


def test_the_unsigned_channel_hint_names_sd_commands_that_parse():
    from harness_manager.services.update.trust import TrustStore

    with pytest.raises(RefusedError) as e:
        TrustStore(pinned=()).verify_for_channel(b"{}", b"", "stable")
    cmds = _backticked(e.value.hint)
    assert [c.split()[:3] for c in cmds] == [["sd", "TARGET", "backup"],
                                             ["sd", "TARGET", "install"]]
    for cmd in cmds:
        make_parser().parse_args(shlex.split(cmd))                # raises UsageError if wrong


def test_twin_the_old_sd_install_hint_does_not_parse():
    with pytest.raises(UsageError):
        make_parser().parse_args(shlex.split("sd install"))
