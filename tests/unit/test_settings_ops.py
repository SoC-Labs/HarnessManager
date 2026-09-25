"""SET-API: the operations the /settings routes and `harness-manager config` share
(``harness_manager.settings.ops``), over real files in a temporary config dir.

Each behaviour has a negative twin: the same call without the deciding condition gives the
other answer, so the check bites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode, HarnessError, RefusedError, UsageError
from harness_manager.settings import ops, testers
from harness_manager.settings.schema import Setting

SECRET = "tok-NEVER-SHOWN-4f1c"
ENV = "HARNESS_MANAGER_OPENOCD"


@pytest.fixture
def world(tmp_path: Path):
    state = tmp_path / "state"
    policy = tmp_path / "policy.toml"
    env: dict[str, str] = {"HARNESS_MANAGER_KEYRING": "off"}
    ctx = ops.SettingsContext(state_dir=state, env=env, policy_path=policy, keyrings=[])
    return ctx, state, policy, env


def text_of(obj) -> str:
    return json.dumps(obj, default=str)


# --- round trips ---------------------------------------------------------------------------------


def test_set_get_unset_round_trip(world):
    ctx, state, _, _ = world
    got = ops.set_values(ctx, {"tools.openocd": "/opt/openocd/bin/openocd",
                               "consoles.scrollback": "8000", "general.theme": "dark"})
    assert got["keys"] == ["tools.openocd", "consoles.scrollback", "general.theme"]
    by_key = {r["key"]: r for r in got["rows"]}
    assert by_key["consoles.scrollback"]["value"] == 8000          # text parsed as the type
    assert by_key["tools.openocd"]["source"] == "user"
    assert "openocd = \"/opt/openocd/bin/openocd\"" in (state / "settings.toml").read_text()
    row = ops.listing(ctx, key="tools.openocd")["rows"][0]
    assert (row["value"], row["source"], row["where"]) == ("/opt/openocd/bin/openocd", "user",
                                                          "settings.toml")
    gone = ops.unset(ctx, "tools.openocd")
    assert gone["changed"] is True and gone["rows"][0]["source"] == "default"
    assert "openocd" not in (state / "settings.toml").read_text()


def test_negative_twin_unset_of_a_value_you_never_set_writes_nothing(world):
    ctx, state, _, _ = world
    got = ops.unset(ctx, "tools.openocd")
    assert got["changed"] is False and not (state / "settings.toml").exists()


def test_a_typed_json_value_must_have_the_type_and_text_is_parsed(world):
    ctx, _, _, _ = world
    assert ops.set_values(ctx, {"consoles.scrollback": 9000})["rows"][0]["value"] == 9000
    assert ops.set_values(ctx, {"hubs.lab.lease_ttl": "90m"})["rows"][0]["value"] == 5400
    for bad in ({"consoles.scrollback": "lots"}, {"consoles.scrollback": 12.5},
                {"tools.openocd": 5}, {"general.theme": "neon"}):
        with pytest.raises(UsageError):
            ops.set_values(ctx, bad)


def test_listing_by_section_and_the_dev_rows_only_on_request(world):
    ctx, _, _, _ = world
    tools = ops.listing(ctx, section="tools")["rows"]
    assert tools and {r["section_id"] for r in tools} == {"tools"}
    kits = ops.listing(ctx, section="Harness + kits")["rows"]
    assert {r["section_id"] for r in kits} == {"harness-kits"}
    plain = {r["key"] for r in ops.listing(ctx)["rows"]}
    every = {r["key"] for r in ops.listing(ctx, include_dev=True)["rows"]}
    assert "dev.no_daemon" in every and "dev.no_daemon" not in plain
    with pytest.raises(UsageError, match="sections: general"):
        ops.listing(ctx, section="nonesuch")


def test_hub_and_board_rows_expand_per_instance(world):
    ctx, _, _, _ = world
    assert not [r for r in ops.listing(ctx, section="hubs")["rows"]]
    ops.set_values(ctx, {"hubs.lab.host": "mapstone-dev.ecs.soton.ac.uk"})
    rows = ops.listing(ctx, section="hubs")["rows"]
    assert rows and all(r["key"].startswith("hubs.lab.") for r in rows)
    assert ops.listing(ctx)["instances"]["hubs"] == ["lab"]


def test_a_pattern_key_is_refused_name_one(world):
    ctx, state, _, _ = world
    with pytest.raises(UsageError, match="name one hub"):
        ops.set_values(ctx, {"hubs.*.host": "x"})
    with pytest.raises(UsageError, match="name one hub"):
        ops.listing(ctx, key="hubs.*.host")
    assert not (state / "settings.toml").exists()


def test_negative_twin_an_unknown_key_is_usage(world):
    ctx, _, _, _ = world
    with pytest.raises(UsageError, match="no such setting"):
        ops.set_values(ctx, {"tools.nonesuch": "x"})


# --- all or nothing ---------------------------------------------------------------------------


def test_one_bad_key_writes_nothing(world):
    ctx, state, _, _ = world
    ops.set_values(ctx, {"general.theme": "light"})
    before = (state / "settings.toml").read_text()
    with pytest.raises(UsageError, match="debug.port_base"):
        ops.set_values(ctx, {"tools.openocd": "/x/openocd", "general.theme": "dark",
                             "debug.port_base": "80"})
    assert (state / "settings.toml").read_text() == before
    assert ops.listing(ctx, key="general.theme")["rows"][0]["value"] == "light"


def test_negative_twin_the_same_keys_all_good_are_all_written(world):
    ctx, state, _, _ = world
    ops.set_values(ctx, {"tools.openocd": "/x/openocd", "general.theme": "dark",
                         "debug.port_base": "30000"})
    text = (state / "settings.toml").read_text()
    assert "/x/openocd" in text and "dark" in text and "30000" in text


def test_a_file_that_does_not_parse_refuses_before_anything_is_written(world):
    ctx, state, _, _ = world
    state.mkdir(parents=True)
    (state / "boards.toml").write_text("[boards.x\nbroken")
    with pytest.raises(UsageError, match="boards.toml is not valid TOML"):
        ops.set_values(ctx, {"general.theme": "dark", "boards.lab.name": "mps3-01"})
    assert not (state / "settings.toml").exists()          # the good file was not written either


# --- locks, env, caps ----------------------------------------------------------------------------


def test_a_locked_key_is_refused_naming_the_policy_file(world):
    ctx, state, policy, _ = world
    policy.write_text('[lock]\ntools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"\n')
    with pytest.raises(RefusedError) as exc:
        ops.set_values(ctx, {"general.theme": "dark", "tools.vivado": "/mine/vivado"})
    assert exc.value.code == ExitCode.REFUSED and str(policy) in exc.value.message
    assert not (state / "settings.toml").exists()           # all or nothing
    row = ops.listing(ctx, key="tools.vivado")["rows"][0]
    assert row["locked"] and row["source"] == "lock" and row["where"] == str(policy)


def test_negative_twin_without_the_lock_the_same_set_is_written(world):
    ctx, _, policy, _ = world
    policy.write_text('[default]\ntools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"\n')
    got = ops.set_values(ctx, {"tools.vivado": "/mine/vivado"})
    assert got["rows"][0]["source"] == "user" and not got["rows"][0]["locked"]


def test_env_shadowing_is_reported(world):
    ctx, _, _, env = world
    ops.set_values(ctx, {"tools.openocd": "/mine/openocd"})
    env[ENV] = "/env/openocd"
    row = ops.listing(ctx, key="tools.openocd")["rows"][0]
    assert (row["value"], row["source"], row["shadowed"]) == ("/env/openocd", "env", f"${ENV}")
    got = ops.set_values(ctx, {"tools.openocd": "/newer/openocd"})
    assert got["rows"][0]["shadowed"] == f"${ENV}" and got["rows"][0]["value"] == "/env/openocd"


def test_negative_twin_env_alone_shadows_nothing(world):
    ctx, _, _, env = world
    env[ENV] = "/env/openocd"
    row = ops.listing(ctx, key="tools.openocd")["rows"][0]
    assert row["source"] == "env" and row["shadowed"] == ""


# --- apply ---------------------------------------------------------------------------------------


def test_the_apply_class_is_the_strongest_change(world):
    ctx, _, _, _ = world
    live = ops.set_values(ctx, {"general.theme": "dark"})
    assert live["apply"] == "live" and live["applies"]["live"] == ["general.theme"]
    reopen = ops.set_values(ctx, {"general.theme": "light", "hubs.lab.host": "hub.example"})
    assert reopen["apply"] == "reopen" and reopen["applies"]["reopen"] == ["hubs.lab.host"]
    restart = ops.set_values(ctx, {"hubs.lab.host": "hub2.example", "advanced.port": "0"})
    assert restart["apply"] == "restart" and restart["applies"]["restart"] == ["advanced.port"]
    assert ops.changed_event(restart) == {"keys": ["hubs.lab.host", "advanced.port"],
                                          "apply": "restart", "applies": restart["applies"],
                                          "source": "api"}


def test_negative_twin_apply_ordering(world):
    assert ops.strongest(["live", "reopen"]) == "reopen"
    assert ops.strongest(["live"]) == "live" and ops.strongest([]) == "live"
    assert ops.strongest(["restart", "live"]) == "restart"


# --- secrets -------------------------------------------------------------------------------------


def test_a_secret_is_stored_and_only_its_status_comes_back(world):
    ctx, state, _, _ = world
    got = ops.set_secret(ctx, "hubs.lab.token", SECRET)
    assert got["secret"]["set"] is True and got["secret"]["backend"] == "file"
    assert got["rows"][0]["value"] == {"set": True}
    for reply in (got, ops.listing(ctx), ops.listing(ctx, key="hubs.lab.token"),
                  ops.schema(ctx), ops.paths(ctx)):
        assert SECRET not in text_of(reply)
    assert SECRET not in (state / "secrets" / "index.json").read_text()
    assert ctx.store().get("hubs.lab.token") == SECRET              # but it is stored
    gone = ops.delete_secret(ctx, "hubs.lab.token")
    assert gone["changed"] is True and gone["secret"]["set"] is False


def test_negative_twin_a_secret_through_set_values_is_refused_and_not_echoed(world):
    ctx, state, _, _ = world
    with pytest.raises(UsageError) as exc:
        ops.set_values(ctx, {"hubs.lab.token": SECRET})
    assert "set-secret" in exc.value.message and SECRET not in str(exc.value)
    assert not (state / "settings.toml").exists()
    with pytest.raises(UsageError, match="is not a secret"):
        ops.set_secret(ctx, "tools.openocd", SECRET)


def test_a_bad_secret_value_is_refused_without_repeating_it(world):
    ctx, _, _, _ = world
    with pytest.raises(UsageError) as exc:
        ops.set_secret(ctx, "updates.github_token", f"{SECRET}\nsecond line")
    assert SECRET not in str(exc.value)
    with pytest.raises(UsageError):
        ops.set_secret(ctx, "updates.github_token", 1234)


def test_paths_name_every_file_and_where_a_secret_would_go(world):
    ctx, state, policy, _ = world
    got = ops.paths(ctx)
    files = {f["what"]: f for f in got["files"]}
    assert files["settings"]["path"] == str(state / "settings.toml")
    assert files["policy"]["path"] == str(policy) and files["policy"]["exists"] is False
    assert got["secrets_backend"]["backend"] == "file"
    assert "no keyring" in got["secrets_backend"]["why"]


# --- testers -------------------------------------------------------------------------------------


def test_a_section_without_a_tester_is_not_testable_yet(world):
    ctx, _, _, _ = world
    got = ops.test(ctx, "tools")
    assert got == {"section": "tools", "name": "", "testable": False, "passed": None,
                   "steps": [], "why": "the tools settings are not testable yet"}


def test_negative_twin_a_registered_tester_runs_and_its_report_is_shaped(world):
    ctx, _, _, _ = world
    seen = {}

    def fake(req: testers.TestRequest):
        seen["req"] = req
        return {"ok": False, "steps": [{"step": "reach", "ok": True, "detail": "up"},
                                       {"step": "auth", "ok": False, "detail": "401",
                                        "hint": "a new token"}], "targets": []}

    testers.register("Tools", fake)
    try:
        got = ops.test(ctx, "tools", "x")
    finally:
        testers.unregister("tools")
    assert got["testable"] is True and got["passed"] is False and got["why"] == "auth: 401"
    assert got["targets"] == [] and [s["step"] for s in got["steps"]] == ["reach", "auth"]
    assert seen["req"].section == "tools" and seen["req"].name == "x"
    assert seen["req"].resolver is not None
    assert ops.test(ctx, "tools")["testable"] is False                 # unregistered again


def test_a_tester_that_needs_a_name_refuses_without_one(world):
    ctx, _, _, _ = world
    testers.register("hubs", lambda req: {"ok": True, "steps": []}, needs_name=True)
    try:
        with pytest.raises(UsageError, match="config test hubs NAME"):
            ops.test(ctx, "hubs")
        assert ops.test(ctx, "hubs", "lab")["passed"] is True
    finally:
        testers.unregister("hubs")


def test_the_hub_tester_is_found_by_convention_when_set_hubs_lands(monkeypatch):
    import sys
    import types

    assert testers.CONVENTION["hubs"][:2] == ("harness_manager.settings.hubs", "test_connection")
    mod = types.ModuleType("harness_manager.settings.hubs")
    mod.test_connection = lambda req: {"ok": True, "steps": [{"step": "config", "ok": True}]}
    monkeypatch.setitem(sys.modules, "harness_manager.settings.hubs", mod)
    t = testers.tester_for("hubs")
    assert t is not None and t.job is True and t.needs_name is True
    monkeypatch.setattr(mod, "test_connection", None)
    assert testers.tester_for("hubs") is None                          # the twin


# --- packs -------------------------------------------------------------------------------------


class _Pack:
    def __init__(self, rows):
        self._rows = rows

    def settings(self):
        return self._rows


def test_a_packs_rows_join_the_schema(tmp_path):
    row = Setting("fake.pace_ms", "int", 20, "Consoles", "Console pacing", pack="fake")
    ctx = ops.SettingsContext(state_dir=tmp_path, env={}, policy_path=tmp_path / "p.toml",
                              keyrings=[], packs=lambda: {"fake": _Pack([row])})
    keys = {r["key"] for r in ops.schema(ctx)["rows"]}
    assert "fake.pace_ms" in keys
    assert ops.listing(ctx, key="fake.pace_ms")["rows"][0]["value"] == 20


def test_negative_twin_a_pack_row_off_its_prefix_is_a_problem_not_a_failure(tmp_path):
    row = Setting("tools.sneaky", "int", 1, "Tools", "x", pack="fake")
    ctx = ops.SettingsContext(state_dir=tmp_path, env={}, policy_path=tmp_path / "p.toml",
                              keyrings=[], packs=lambda: {"fake": _Pack([row])})
    got = ops.schema(ctx)
    assert "tools.sneaky" not in {r["key"] for r in got["rows"]}
    assert any("fake pack's settings are not used" in p for p in got["problems"])
    assert any("fake pack" in p for p in ops.listing(ctx)["problems"])


def test_an_error_is_a_harness_error_with_its_exit_code(world):
    ctx, _, _, _ = world
    with pytest.raises(HarnessError) as exc:
        ops.set_values(ctx, {})
    assert exc.value.code == ExitCode.USAGE
