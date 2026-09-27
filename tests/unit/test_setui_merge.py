"""Lane SET-UI-MERGE: the Settings dialog's backend, after SET-UI met SET-WIRE, SLOT-TIMING,
MCC-FIX and DEMO-ALL on main. Each check has its negative twin.

- a number's ``bounds`` (the menu's limits) come from its check, open ends and "more than 0"
  included, and only for numbers;
- the ``--demo`` service's settings are its own: no OS keyring (a demo secret must never
  replace or remove your real one, which the keyring keys by the setting's name), no real
  hub reached (Test connection refused before anything runs), and ``service.demo`` says so;
- the demo pack declares the MPS3 rows, so the demo's dialog shows what an MPS3 install does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_manager.core.errors import UnavailableError, UsageError
from harness_manager.demo import DemoEngine
from harness_manager.settings import ops
from harness_manager.settings.packs import with_packs
from harness_manager.settings.resolve import core_schema
from harness_manager.settings.secrets import SERVICE, KeyringBackend
from harness_manager_mps3.pack import Mps3Pack
from tests.unit.test_settings_secrets import StubKeyring

REAL = "ghp_THE-USERS-REAL-TOKEN"
DEMO = "ghp_a-token-typed-into-the-demo"


def schema_rows() -> dict:
    return {r.key: r for r in with_packs(core_schema(), [Mps3Pack()])[0].rows}


# --- bounds --------------------------------------------------------------------------------------


def test_the_slot_rows_bounds_say_more_than_0_an_ints_as_1_and_the_pace_rows_their_range():
    rows = schema_rows()
    view = {k: rows[k].view() for k in rows}
    assert (view["mps3.slot.card_write_bps"]["bounds"], view["mps3.slot.card_write_bps"]["min_exclusive"]) \
        == ([1, None], False)
    assert (view["mps3.slot.card_read_bps"]["bounds"], view["mps3.slot.card_read_bps"]["min_exclusive"]) \
        == ([1, None], False)
    for k in ("mps3.slot.job_timeout_s", "mps3.slot.push_timeout_s"):
        assert (view[k]["bounds"], view[k]["min_exclusive"]) == ([0, None], True), k
    assert view["mps3.console.pace_ms"]["bounds"] == [0, 500]
    assert view["mps3.mcc.pace_ms"]["bounds"] == [50, 1000]
    assert view["consoles.font_size"]["bounds"] == [8, 32]          # rows._between, as before
    for k, r in rows.items():                          # every bound is its check's own edge
        v = r.view()
        if not v["bounds"]:
            continue
        lo, hi = v["bounds"]
        step = 1 if r.type == "int" else 0.001
        if lo is not None:
            first = lo + step if v["min_exclusive"] else lo
            assert r.check(first) == "" and r.check(first - step) != "", k
        if hi is not None:
            assert r.check(hi) == "" and r.check(hi + step) != "", k


def test_negative_twin_a_row_that_is_not_a_number_has_no_bounds_even_with_a_positive_check():
    rows = schema_rows()
    assert rows["hubs.*.request_ttl"].type == "duration" and rows["hubs.*.request_ttl"].check
    assert rows["hubs.*.request_ttl"].view()["bounds"] is None
    assert rows["hubs.*.stage_dir"].view()["bounds"] is None            # a str with a check
    assert rows["general.window_size"].view()["bounds"] is None


# --- the demo service's settings -------------------------------------------------------------------


def context(tmp_path: Path, *, demo: bool, stub: StubKeyring) -> ops.SettingsContext:
    """A service's context as ``run_daemon`` leaves it, with a keyring this process can reach."""
    ctx = ops.SettingsContext(state_dir=tmp_path / "svc", env={},
                              policy_path=tmp_path / "policy.toml",
                              keyrings=[KeyringBackend("secret-service", impl=stub, env={})])
    ctx.demo = demo
    (tmp_path / "svc").mkdir(exist_ok=True)
    return ctx


def test_the_demo_stores_a_secret_in_its_own_files_and_never_touches_the_keyring(tmp_path):
    stub = StubKeyring()
    stub.items[(SERVICE, "updates.github_token")] = REAL           # the user's real token
    ctx = context(tmp_path, demo=True, stub=stub)
    got = ops.set_secret(ctx, "updates.github_token", DEMO)
    assert got["secret"]["backend"] == "file"
    assert (tmp_path / "svc" / "secrets").is_dir()
    assert ctx.store().get("updates.github_token") == DEMO
    ops.delete_secret(ctx, "updates.github_token")                  # Remove, in the demo
    assert stub.items[(SERVICE, "updates.github_token")] == REAL and stub.calls == []


def test_negative_twin_outside_the_demo_a_reachable_keyring_takes_it(tmp_path):
    stub = StubKeyring()
    ctx = context(tmp_path, demo=False, stub=stub)
    assert ops.set_secret(ctx, "updates.github_token", DEMO)["secret"]["backend"] == "secret-service"
    assert stub.items[(SERVICE, "updates.github_token")] == DEMO


def test_the_demo_refuses_a_hub_test_before_anything_runs_and_says_so(tmp_path):
    ctx = context(tmp_path, demo=True, stub=StubKeyring())
    (tmp_path / "svc" / "settings.toml").write_text('[hubs.lab]\ntransport = "ssh"\n')
    with pytest.raises(UnavailableError) as e:
        ops.test(ctx, "hubs", "lab")
    assert "the demo reaches no real hub" in e.value.message
    assert ops.listing(ctx)["service"] == {"demo": True}


def test_negative_twin_outside_the_demo_the_hub_test_runs(tmp_path):
    ctx = context(tmp_path, demo=False, stub=StubKeyring())
    # no host: the config step fails, so nothing is run (no ssh)
    (tmp_path / "svc" / "settings.toml").write_text('[hubs.lab]\ntransport = "ssh"\n')
    rep = ops.test(ctx, "hubs", "lab")
    assert rep["passed"] is False and rep["failed"] == "config"
    assert ops.listing(ctx)["service"] == {"demo": False}


def test_the_demo_engine_declares_the_mps3_rows_so_its_dialog_shows_them(tmp_path):
    eng = DemoEngine(speed=0.25)
    try:
        ctx = ops.SettingsContext(state_dir=tmp_path, env={}, engine=eng,
                                  policy_path=tmp_path / "policy.toml")
        keys = {r["key"] for r in ops.schema(ctx)["rows"]}
        assert {"mps3.slot.card_write_bps", "mps3.slot.job_timeout_s", "mps3.console.pace_ms",
                "boards.*.hub.shares.*"} <= keys
        row = ops.listing(ctx, key="mps3.slot.job_timeout_s")["rows"][0]
        assert (row["value"], row["source"]) == (1800.0, "pack")
    finally:
        eng.close_all()


def test_negative_twin_a_context_without_an_engine_has_the_cores_rows_only(tmp_path):
    ctx = ops.SettingsContext(state_dir=tmp_path, env={}, policy_path=tmp_path / "policy.toml")
    keys = {r["key"] for r in ops.schema(ctx)["rows"]}
    assert not any(k.startswith("mps3.") for k in keys)


# --- MCC-FIX in the settings: no share on tty_00; shares.mcc only names the MCC's path ------------

TTY00 = "/dev/mps3_01_pl/tty_00"
REASON = "tty_00 is the MCC console; Harness Manager never shares it; the MCC is reached on the hub"


def pack_context(tmp_path: Path, boards: str = "") -> ops.SettingsContext:
    ctx = ops.SettingsContext(state_dir=tmp_path, env={}, policy_path=tmp_path / "policy.toml")
    ctx._layers = with_packs(core_schema(), [Mps3Pack()])
    if boards:
        (tmp_path / "boards.toml").write_text(boards)
    return ctx


@pytest.mark.parametrize("tty", [TTY00, "/dev/mps3_02_pl/tty_00", "/dev/serial/by-id/x/tty_00/"])
def test_a_share_on_tty_00_is_refused_by_config_set_and_skipped_in_the_files(tmp_path, tty):
    ctx = pack_context(tmp_path, f'[boards.lab]\nhub = {{ target = "mps3_01_pl", shares = '
                                 f'{{ fpga_uart0 = "{tty}" }} }}\n')
    with pytest.raises(UsageError) as e:
        ops.set_values(ctx, {"boards.lab.hub.shares.fpga_uart1": tty})
    assert REASON in e.value.message and "boards.lab.hub.shares.fpga_uart1" in e.value.message
    assert "fpga_uart1" not in (tmp_path / "boards.toml").read_text()      # nothing written
    row = ops.listing(ctx, key="boards.lab.hub.shares.fpga_uart0")["rows"][0]
    assert row["value"] is None and row["source"] == "default"               # the file's: skipped
    assert any(REASON in p and p.endswith("; ignored") for p in row["problems"])


def test_negative_twin_a_lane_share_on_tty_01_and_the_mccs_own_path_name_are_accepted(tmp_path):
    ctx = pack_context(tmp_path, f'[boards.lab]\nhub = {{ target = "mps3_01_pl", shares = '
                                 f'{{ mcc = "{TTY00}" }} }}\n')
    ops.set_values(ctx, {"boards.lab.hub.shares.fpga_uart1": "/dev/mps3_01_pl/tty_01"})
    mcc = ops.listing(ctx, key="boards.lab.hub.shares.mcc")["rows"][0]
    assert (mcc["value"], mcc["source"], mcc["problems"]) == (TTY00, "user", [])
    lane = ops.listing(ctx, key="boards.lab.hub.shares.fpga_uart1")["rows"][0]
    assert (lane["value"], lane["problems"]) == ("/dev/mps3_01_pl/tty_01", [])
    ops.set_values(ctx, {"boards.lab.hub.shares.mcc": TTY00})               # its path name: kept


def test_adopting_an_inline_hub_with_a_share_on_tty_00_is_refused_and_nothing_changes(tmp_path):
    from harness_manager.settings import hubs

    text = (f'[boards.lab]\nhub = {{ host = "hub.invalid", target = "mps3_01_pl", shares = '
            f'{{ fpga_uart0 = "{TTY00}" }} }}\n')
    ctx = pack_context(tmp_path, text)
    with pytest.raises(UsageError) as e:
        hubs.adopt_inline_hub("lab", ctx.resolver())
    assert REASON in e.value.message and "boards.toml is not changed" in e.value.message
    assert (tmp_path / "boards.toml").read_text() == text
    assert not list(tmp_path.glob("boards.toml.bak-*")) and not (tmp_path / "settings.toml").exists()


def test_negative_twin_adopting_one_with_a_lane_share_and_the_mccs_path_name_works(tmp_path):
    from harness_manager.settings import hubs

    ctx = pack_context(tmp_path, f'[boards.lab]\nhub = {{ host = "hub.invalid", target = "mps3_01_pl", '
                                 f'shares = {{ mcc = "{TTY00}", fpga_uart1 = "/dev/mps3_01_pl/tty_01" }} }}\n')
    got = hubs.adopt_inline_hub("lab", ctx.resolver())
    assert got["changed"] is True and got["hub"] == "hub"
    text = (tmp_path / "boards.toml").read_text()
    assert 'use = "hub"' in text and TTY00 in text and "tty_01" in text
