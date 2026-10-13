"""L2 CARD-CAPACITY: the card's slot size and layout (net-protocol v0.20), the size refusal,
the engine's "bad args" on an over-size commit, and the Linux reboot budget."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.core.pack import CardStatus
from harness_manager.services import slots as slots_mod
from harness_manager.services.slots import card_status_from_json, card_status_json
from harness_manager_mps3 import card as card_mod
from harness_manager_mps3 import shell as sh
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t2_overlays import make_overlay, point_pushes_at, use_overlay_dirs
from tests.fakes.virtual_board import LINUX_MINT4, VirtualMps3, linux_mint4_profile

SENTENCE = ("This card's design slots are 8 MiB. Re-make the card in layout v2 "
            "(32 MiB slots) to keep this design on it.")
JS = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js/sections"


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


def _open(vb, monkeypatch, tmp_path, partial_len=None):
    kw = {} if partial_len is None else {"partial": b"\x5a" * partial_len}
    make_overlay(tmp_path / "ov", "synth", static_id=vb.profile.static_id, **kw)
    use_overlay_dirs(monkeypatch, tmp_path / "ov")
    point_pushes_at(monkeypatch, vb)
    session = Mps3Pack(console_ports=vb.console_ports).open(vb.candidate())
    ref = next(r for r in session.deploy.overlays() if r.name == "synth"
               and int(r.static_id, 16) == vb.profile.static_id)
    return session, ref


@pytest.mark.parametrize("profile,want", [
    (LINUX_MINT4, (32, 2)),
    (linux_mint4_profile(usd_slot_mib=8), (8, 1)),
    (linux_mint4_profile(usd_report_store_ver=False), (None, None)),
])
def test_card_status_reads_slot_mb_and_store_ver(tmp_path, monkeypatch, profile, want):
    with VirtualMps3(tmp_path, profile) as vb:
        vb.shell.usd_insert("da")
        session, _ = _open(vb, monkeypatch, tmp_path)
        st = session.deploy.card_status()
    assert (st.slot_mb, st.store_ver) == want
    back = card_status_from_json(card_status_json(st))      # the daemon's JSON round trip
    assert (back.slot_mb, back.store_ver) == want


def test_over_size_design_is_refused_on_an_8_mib_card_before_any_byte(tmp_path, monkeypatch):
    big = 9 * 1024 * 1024
    with VirtualMps3(tmp_path, linux_mint4_profile(usd_slot_mib=8)) as vb:
        vb.shell.usd_insert("da")
        session, ref = _open(vb, monkeypatch, tmp_path, partial_len=big)
        with pytest.raises(RefusedError) as exc:
            session.deploy.deploy(ref, keep_on_card=True)
        commits = list(vb.shell.commits)
    assert exc.value.message == SENTENCE and commits == []


def test_over_size_design_is_refused_when_the_shell_reports_nothing(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, linux_mint4_profile(usd_report_store_ver=False)) as vb:
        vb.shell.usd_insert("da")
        session, ref = _open(vb, monkeypatch, tmp_path, partial_len=9 * 1024 * 1024)
        with pytest.raises(RefusedError) as exc:
            session.deploy.deploy(ref, keep_on_card=True)
    assert exc.value.message == SENTENCE            # absent means 8 MiB


def test_negative_twin_a_small_design_is_kept_on_an_8_mib_card(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, linux_mint4_profile(usd_slot_mib=8)) as vb:
        vb.shell.usd_insert("da")
        session, ref = _open(vb, monkeypatch, tmp_path)
        result = session.deploy.deploy(ref, keep_on_card=True)
    assert result.card.kept


def test_capacity_rule():
    mib = 1024 * 1024
    f = card_mod.slot_capacity_refusal
    assert f(None, 8 * mib) == "" and f(None, 8 * mib + 1) == SENTENCE
    assert f(8, 7_490_000) == "" and f(32, 20 * mib) == ""
    assert "32 MiB" in f(32, 33 * mib) and f(32, 33 * mib) != SENTENCE
    assert f(8, None) == "" and f(8, 0) == ""


def test_bad_args_on_a_commit_means_the_slot_is_too_small():
    err = card_mod._usd_error("card commit of multicore", "bad args")
    assert isinstance(err, RefusedError) and err.message == SENTENCE


def test_negative_twin_bad_args_elsewhere_stays_a_failure():
    err = card_mod._usd_error("card clear", "bad args")
    assert not isinstance(err, RefusedError)


def test_reboot_budget_is_longer_than_a_linux_cold_boot():
    from harness_manager.daemon import card_api
    from harness_manager_mps3 import constants

    assert slots_mod.REBOOT_WAIT_S == slots_mod.REBOOT_WAIT_S_LINUX == constants.REBOOT_WAIT_S_LINUX
    assert card_api.REBOOT_WAIT_S > 205
    src = (Path(slots_mod.__file__).parent / "update/executor.py").read_text()
    assert "or 180.0" not in src and "reboot_wait_s or REBOOT_WAIT_S_LINUX" in src


def test_card_status_json_carries_the_layout():
    doc = card_status_json(CardStatus(store=True, present=True, slot_mb=32, store_ver=2))
    assert (doc["slot_mb"], doc["store_ver"]) == (32, 2)
    old = card_status_json(CardStatus(store=True, present=True))
    assert old["slot_mb"] is None and old["store_ver"] is None


def test_versions_subtitle_uses_the_slot_state_not_the_version():
    src = (JS / "update.js").read_text()
    start = src.index("export function versionsStatus")
    body = src[start:start + 3000]
    assert "sl.state === \"empty\"" in body
    assert not re.search(r"\.version\)\);\s*\n\s*if \(empty\)", body)


def test_layout_text_in_boardfacts_and_the_versions_page():
    bf = (JS / "boardfacts.js").read_text()
    assert "export function cardLayoutText" in bf and "MiB design slots" in bf
    assert "cardLayoutText" in (JS / "update.js").read_text()
