"""N1: board display names. The resolution order, boards.toml ``name``, the harness name,
and the hub's name for the board. Each check has a negative twin."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager import naming
from harness_manager.core.errors import UnreachableError
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.power.config import ConfigError, load_boards, with_links
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import naming as mnaming
from harness_manager_mps3.identify import parse_reply
from harness_manager_mps3.shell import ShellLive
from tests.fakes.l1_fake_hub import FakeHub

HUB = "mapstone-dev.ecs.soton.ac.uk"
ADDR = "192.168.10.101:6900"


@pytest.fixture(autouse=True)
def _fresh_cache():
    mnaming.clear_cache()
    yield
    mnaming.clear_cache()


def cand(**kw) -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{ADDR}",
                     links=(Link(LinkKind.ETHERNET, ADDR, "shell control channel"),), **kw)


def boards_toml(text: str) -> Path:
    state = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    path = state / "boards.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- the order ------------------------------------------------------------------------------


def test_the_order_is_config_then_harness_then_hub_then_the_derived_hub_name():
    assert naming.SOURCES == ("config", "harness", "hub", "hub-target")
    c = naming.offer(cand(), "mps3-01", naming.HUB_TARGET)
    c = naming.offer(c, "mps3-01", naming.HUB)
    assert (c.name, c.name_source) == ("mps3-01", "hub")
    c = naming.offer(c, "unit-7", naming.HARNESS)
    assert (c.name, c.name_source) == ("unit-7", "harness")
    c = naming.offer(c, "lab board", naming.CONFIG)
    assert (c.name, c.name_source) == ("lab board", "config")


def test_negative_twin_a_weaker_source_never_renames():
    c = naming.offer(cand(), "lab board", naming.CONFIG)
    for source in (naming.HARNESS, naming.HUB, naming.HUB_TARGET):
        assert naming.offer(c, "other", source) is c
    hub = naming.offer(cand(), "mps3-01", naming.HUB)
    assert naming.offer(hub, "mps3-99", naming.HUB_TARGET) is hub


def test_the_same_source_may_rename_a_board():
    c = naming.offer(cand(), "old", naming.HARNESS)
    assert naming.offer(c, "new", naming.HARNESS).name == "new"


def test_negative_twin_an_unknown_source_or_a_bad_name_is_ignored():
    c = cand()
    assert naming.offer(c, "mps3-01", "dns") is c
    for bad in ("", "   ", "x" * 65, "bad\nname", "tab\tname", "\x1b[31mred", 42, None):
        assert naming.offer(c, bad, naming.CONFIG) is c, bad


def test_clean_name_trims_and_keeps_printable_unicode():
    assert naming.clean_name("  mps3-01 ") == "mps3-01"
    assert naming.clean_name("Böard №1") == "Böard №1"
    assert naming.clean_name("x" * 64) == "x" * 64
    assert naming.clean_name("a​b") == ""          # a zero-width (format) character


def test_display_name_falls_back_to_the_address():
    assert naming.display_name(cand()) == ADDR
    assert naming.display_name(naming.offer(cand(), "mps3-01", naming.HUB)) == "mps3-01"
    assert naming.describe_source(naming.offer(cand(), "x", naming.CONFIG)) == "from boards.toml"
    assert naming.describe_source(cand()) == ""


def test_the_harness_name_comes_from_the_identity():
    c = naming.with_identity(cand(), BoardIdentity(board_type="mps3", name="unit-7"))
    assert (c.name, c.name_source) == ("unit-7", "harness")
    plain = cand()
    assert naming.with_identity(plain, BoardIdentity(board_type="mps3")) is plain
    assert naming.with_identity(plain, None) is plain


def test_stronger_keeps_the_better_name_of_two_candidates():
    a = naming.offer(cand(), "mps3-01", naming.HUB_TARGET)
    b = naming.offer(cand(), "lab", naming.CONFIG)
    assert naming.stronger(a, b) == ("lab", "config") == naming.stronger(b, a)
    assert naming.stronger(a, cand()) == ("mps3-01", "hub-target")
    assert naming.stronger(cand(), cand()) == ("", "")


def test_a_hub_id_is_shown_with_hyphens():
    assert naming.hub_display("mps3_01") == "mps3-01"
    assert naming.hub_display("mps3-01") == "mps3-01"


# --- boards.toml ---------------------------------------------------------------------------


def test_boards_toml_name_names_the_board_matched_by_address():
    boards_toml('[boards.lab]\nmatch = ["192.168.10.101"]\nname = "mps3-01"\n')
    cfg = load_boards()
    assert cfg.boards[0].name == "mps3-01" and "name" not in cfg.boards[0].tables
    assert naming.config_name(cand()) == "mps3-01"
    c = naming.with_config_name(cand())
    assert (c.name, c.name_source) == ("mps3-01", "config")


def test_negative_twin_another_boards_name_or_no_name_does_not_apply():
    boards_toml('[boards.other]\nmatch = ["192.168.10.102"]\nname = "mps3-02"\n'
                '[boards.lab]\nmatch = ["192.168.10.101"]\n')
    assert naming.config_name(cand()) == ""
    assert naming.with_config_name(cand()).name == ""


def test_a_bad_boards_toml_name_is_logged_and_ignored_not_fatal(caplog):
    boards_toml('[boards.lab]\nmatch = ["192.168.10.101"]\nname = 42\nvia = "ssh:hub"\n')
    board = load_boards().boards[0]                    # the file still loads: via still routes
    assert board.name == "" and board.tables["via"] == "ssh:hub"
    assert "boards.'lab'.name" in caplog.text
    assert naming.config_name(cand()) == ""


def test_negative_twin_a_broken_boards_toml_still_fails_loudly_and_naming_falls_back():
    boards_toml('[boards.lab]\nmatch = 7\nname = "mps3-01"\n')
    with pytest.raises(ConfigError):
        load_boards()
    assert naming.config_name(cand()) == ""            # the display falls back, never raises


def test_with_links_keeps_the_name():
    c = naming.offer(cand(), "mps3-01", naming.CONFIG)
    out = with_links(c, [Link(LinkKind.SMART_POWER, "http://plug", "plug")])
    assert (out.name, out.name_source) == ("mps3-01", "config") and len(out.links) == 2


# --- the harness says it (the proposed `name` key) --------------------------------------------


def test_version_name_becomes_the_identity_name():
    class Ver:
        ok = True
        features: tuple[str, ...] = ()
        skew_verdict = "unchecked"
        harness, sha, dirty, lmb_kb = "1.0.0", "abc", False, 1024

    live = ShellLive("0x72bb0a36", "0x00000000", version=Ver(),
                     raw_version={"ok": True, "name": "mps3-01"})
    assert live.identity().name == "mps3-01"
    bad = ShellLive("0x72bb0a36", "0x00000000", version=Ver(), raw_version={"name": "a\nb"})
    assert bad.identity().name == ""
    assert ShellLive("0x72bb0a36", "0x00000000").identity().name == ""   # no version verb


def test_identify_name_is_read_and_ignored_in_rescue():
    nonce = "0123456789abcdef"
    run = {"ok": True, "op": "identify", "v": 1, "nonce": nonce, "name": "mps3-01",
           "shell_id": "0x72bb0a36", "rm_id": "0x0", "harness": "1.0.0"}
    reply = parse_reply(json.dumps(run).encode(), nonce, ("192.168.10.101", 6899))
    assert reply is not None and reply.name == "mps3-01" and reply.identity().name == "mps3-01"
    rescue = parse_reply(json.dumps({**run, "mode": "rescue"}).encode(), nonce,
                         ("192.168.10.101", 6899))
    assert rescue is not None and rescue.name == "" and rescue.identity().name == ""


# --- the hub (fpgahub 0.3.0 `board list --json`) ------------------------------------------------


GROUPS = {"groups": [
    {"board": "pynq_z2_01", "size": 2, "is_paired": True,
     "members": [{"name": "pynq_z2_01_ps", "role": "ps"}, {"name": "pynq_z2_01_pl", "role": "pl"}]},
    {"board": "mps3_01", "size": 1, "is_paired": False,
     "members": [{"name": "mps3_01_pl", "role": "pl"}]},
]}


def test_the_board_that_owns_the_target_is_read_from_board_list():
    assert mnaming.parse_board_list(json.dumps(GROUPS, indent=2), "mps3_01_pl") == "mps3_01"
    assert mnaming.parse_board_list(json.dumps(GROUPS), "pynq_z2_01_ps") == "pynq_z2_01"


def test_negative_twin_a_target_no_board_owns_gives_no_name_and_junk_raises():
    assert mnaming.parse_board_list(json.dumps(GROUPS), "mps3_02_pl") == ""
    with pytest.raises(ValueError):
        mnaming.parse_board_list("no active boards\n", "mps3_01_pl")
    with pytest.raises(ValueError):
        mnaming.parse_board_list(json.dumps({"boards": []}), "mps3_01_pl")


def test_the_derived_board_id_follows_fpgahubs_suffix_rule():
    assert mnaming.derive_board_id("mps3_01_pl") == "mps3_01"
    assert mnaming.derive_board_id("kr260_01_ps") == "kr260_01"
    assert mnaming.derive_board_id("mps3_01_mcc") == "mps3_01"
    assert mnaming.derive_board_id("mps3_01") == "mps3_01"        # no role suffix: itself
    assert mnaming.derive_board_id("_pl") == "_pl"                # a bare suffix is not stripped


def test_the_fake_hub_answers_board_list_as_fpgahub_does():
    fake = FakeHub("mps3_01_pl")
    res = fake(["fpgahub", "board", "list", "--json"])
    assert res.returncode == 0 and json.loads(res.stdout) == {"groups": [
        {"board": "mps3_01", "size": 1, "is_paired": False,
         "members": [{"name": "mps3_01_pl", "role": "pl"}]}]}


def test_the_hub_is_asked_once_and_the_answer_is_cached(monkeypatch):
    fake = FakeHub("mps3_01_pl")
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group: fake)
    assert mnaming.hub_board_id(HUB, "mps3_01_pl") == "mps3_01"
    assert mnaming.hub_board_id(HUB, "mps3_01_pl") == "mps3_01"
    assert fake.calls == [["fpgahub", "board", "list", "--json"]]
    assert mnaming.cached_board_id(HUB, "mps3_01_pl") == "mps3_01"


def test_negative_twin_a_failing_hub_gives_no_name_and_is_not_asked_again_at_once(monkeypatch):
    fake = FakeHub("mps3_01_pl")
    fake.fail_with = "ssh: connect to host mapstone-dev port 22: Connection timed out"
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group: fake)
    with pytest.raises(UnreachableError):
        mnaming.query_board_id(HUB, "mps3_01_pl")
    fake.fail_with = "ssh: connect to host mapstone-dev port 22: Connection timed out"
    assert mnaming.hub_board_id(HUB, "mps3_01_pl") == ""
    assert mnaming.hub_board_id(HUB, "mps3_01_pl") == ""
    assert len(fake.calls) == 2                        # the query above, then ONE cached miss


def test_lr_a_board_id_is_used_when_the_hub_client_has_it(monkeypatch):
    """CCR N1-4: once lane LR-A's HubClient.board_id() lands, naming asks it, not board list."""
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda *a: pytest.fail("board list ran"))

    class Client:
        def board_id(self) -> str:
            return "mps3_09"

    assert mnaming.hub_board_id(HUB, "mps3_09_pl", client=Client()) == "mps3_09"


LAB = f'[boards.lab]\nmatch = ["192.168.10.101"]\nhub = {{ host = "{HUB}", target = "mps3_01_pl" }}\n'


def test_the_hub_table_names_a_board_before_it_is_opened_without_asking_the_hub(monkeypatch):
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda *a: pytest.fail("hub asked"))
    boards_toml(LAB)
    c = mnaming.name_candidate(cand())
    assert (c.name, c.name_source) == ("mps3-01", "hub-target")


def test_a_cached_hub_answer_outranks_the_derived_name(monkeypatch):
    boards_toml(LAB)
    fake = FakeHub("mps3_01_pl")
    fake.boards = {"lab_mps3_a": [("mps3_01_pl", "pl")]}      # an explicit chassis on the hub
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group: fake)
    assert mnaming.hub_board_id(HUB, "mps3_01_pl") == "lab_mps3_a"
    c = mnaming.name_candidate(cand())
    assert (c.name, c.name_source) == ("lab-mps3-a", "hub")


def test_boards_toml_hub_board_states_the_hubs_name():
    boards_toml(LAB.replace('target = "mps3_01_pl"', 'target = "mps3_01_pl", board = "mps3_01"'))
    c = mnaming.name_candidate(cand())
    assert (c.name, c.name_source) == ("mps3-01", "hub")


def test_negative_twin_no_hub_table_no_hub_name():
    boards_toml('[boards.lab]\nmatch = ["192.168.10.101"]\n')
    assert mnaming.name_candidate(cand()).name == ""
    boards_toml(LAB + 'name = "bench board"\n')
    c = mnaming.name_candidate(cand())
    assert (c.name, c.name_source) == ("bench board", "config")    # config beats the hub


def test_the_session_hook_asks_the_hub_for_an_open_board(monkeypatch):
    fake = FakeHub("mps3_01_pl")
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group: fake)

    class Session:
        candidate = cand()
        hub = hubmod.Mps3Hub(hubmod.HubConfig(host=HUB, target="mps3_01_pl"))

    assert mnaming.session_board_name(Session()) == ("mps3-01", "hub")
    assert mnaming.session_board_name(type("NoHub", (), {"candidate": cand(), "hub": None})()) \
        == ("", "")
