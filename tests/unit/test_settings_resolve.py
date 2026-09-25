"""SET-CORE: the resolver's precedence, caps and failure handling (spike S1, promoted).

policy [lock] > env > user file > policy [default] > pack default > built-in; then a cap.
Every probe has a negative twin: the same setup without the deciding layer, which must give
a different answer (so the probe bites).
"""

from __future__ import annotations

import json
from itertools import combinations

import pytest

from harness_manager.core.errors import ExitCode, RefusedError, UsageError
from harness_manager.settings import MachinePolicy, Resolver, parse_machine_policy
from harness_manager.settings.secrets import FileBackend, SecretStore

POL = "/etc/harness-manager/policy.toml"
KEY = "tools.openocd"                       # admin-lockable, env HARNESS_MANAGER_OPENOCD
ENV = "HARNESS_MANAGER_OPENOCD"
VALUES = {"lock": "/lock/openocd", "env": "/env/openocd", "user": "/user/openocd",
          "machine": "/machine/openocd", "pack": "/pack/openocd"}
ORDER = ("lock", "env", "user", "machine", "pack")


def resolver(layers, **kw) -> Resolver:
    """A resolver with exactly the named layers set for KEY."""
    return Resolver(
        policy=MachinePolicy(POL, lock={KEY: VALUES["lock"]} if "lock" in layers else {},
                             default={KEY: VALUES["machine"]} if "machine" in layers else {}),
        env={ENV: VALUES["env"]} if "env" in layers else {},
        user={KEY: VALUES["user"]} if "user" in layers else {},
        pack_defaults={KEY: VALUES["pack"]} if "pack" in layers else {}, **kw)


def test_nothing_set_is_the_built_in_default():
    got = resolver(()).resolve(KEY)
    assert (got.source, got.value, got.locked, got.shadowed) == ("default", "", False, "")


@pytest.mark.parametrize("layer", ORDER)
def test_each_layer_alone_wins_and_says_where(layer):
    got = resolver((layer,)).resolve(KEY)
    assert got.source == layer and got.value == VALUES[layer]
    assert got.locked == (layer == "lock")
    assert got.where == {"lock": POL, "env": f"${ENV}", "user": "settings.toml",
                         "machine": POL, "pack": "the board pack"}[layer]


@pytest.mark.parametrize("hi, lo", list(combinations(ORDER, 2)))
def test_each_pair_the_higher_layer_wins(hi, lo):
    assert resolver((hi, lo)).resolve(KEY).source == hi


@pytest.mark.parametrize("hi, lo", list(combinations(ORDER, 2)))
def test_negative_twin_each_pair_without_the_higher_layer_the_lower_wins(hi, lo):
    assert resolver((lo,)).resolve(KEY).source == lo


def test_all_layers_at_once_the_lock_wins():
    got = resolver(ORDER).resolve(KEY)
    assert got.source == "lock" and got.value == VALUES["lock"] and got.locked


def test_env_over_the_user_says_it_shadows_the_users_value():
    got = resolver(("env", "user")).resolve(KEY)
    assert got.source == "env" and got.shadowed == f"${ENV}"


def test_negative_twin_env_alone_shadows_nothing():
    assert resolver(("env",)).resolve(KEY).shadowed == ""


def test_the_update_channel_keeps_todays_order_the_user_beats_the_variable():
    env = {"HARNESS_MANAGER_UPDATE_CHANNEL": "beta"}
    got = Resolver(env=env, user={"updates.channel": "dev"}).resolve("updates.channel")
    assert got.source == "user" and got.value == "dev"
    # twin: without the user's choice the variable is used, above the default
    got = Resolver(env=env, user={}).resolve("updates.channel")
    assert got.source == "env" and got.value == "beta"


# --- locks -----------------------------------------------------------------------------------


def test_setting_a_locked_key_is_refused_naming_the_file():
    r = resolver(("lock",))
    with pytest.raises(RefusedError) as exc:
        r.set(KEY, "/elsewhere")
    assert exc.value.code == ExitCode.REFUSED and POL in exc.value.message


def test_negative_twin_without_the_lock_the_same_set_is_stored():
    r = resolver(())
    got = r.set(KEY, "/elsewhere")
    assert got.source == "user" and got.value == "/elsewhere"


def test_a_pattern_lock_covers_every_board_and_names_the_pattern():
    pol = MachinePolicy(POL, lock={"boards.*.power.cycle": False})
    pol = MachinePolicy(POL, lock=pol.lock)
    r = Resolver(policy=pol, env={}, user={'boards."mps3@1.2.3.4:6900".power.cycle': True})
    # boards.*.power.cycle is a user row: the admin cannot lock it (a problem says so)
    got = r.resolve('boards."mps3@1.2.3.4:6900".power.cycle')
    assert got.source == "user" and not got.locked
    assert any("power.cycle" in p and "not the administrator" in p for p in r.problems)
    r = Resolver(policy=MachinePolicy(POL, lock={"hubs.*.lease_ttl": "2h"}), env={}, user={})
    got = r.resolve("hubs.lab.lease_ttl")
    assert got.locked and got.value == 7200
    with pytest.raises(RefusedError, match=r"\[lock\] hubs\.\*\.lease_ttl"):
        r.set("hubs.other.lease_ttl", 60)


def test_a_bad_lock_fails_closed_locked_at_the_default_with_the_reason():
    r = Resolver(policy=MachinePolicy(POL, lock={"updates.channel": "Nightly!"}), env={},
                 user={"updates.channel": "beta"})
    got = r.resolve("updates.channel")
    assert got.locked and got.value == "stable" and "not valid" in got.problems[0]


def test_negative_twin_a_good_lock_is_its_value_without_problems():
    r = Resolver(policy=MachinePolicy(POL, lock={"updates.channel": "beta"}), env={}, user={})
    got = r.resolve("updates.channel")
    assert got.locked and got.value == "beta" and not got.problems


def test_a_lock_on_a_user_row_is_ignored_with_a_problem():
    r = Resolver(policy=MachinePolicy(POL, lock={"general.theme": "dark"}), env={},
                 user={"general.theme": "light"})
    assert r.resolve("general.theme").value == "light"
    assert any("general.theme" in p for p in r.problems)


# --- caps (ceilings) -------------------------------------------------------------------------


def test_the_self_update_cap_lowers_the_users_mode_and_says_why():
    r = Resolver(policy=MachinePolicy(POL, cap={"updates.auto": "notify"}), env={},
                 user={"updates.auto": "stage"})
    got = r.resolve("updates.auto")
    assert got.value == "notify" and "at most 'notify'" in got.capped and not got.locked


def test_negative_twin_a_mode_below_the_cap_is_left_alone():
    r = Resolver(policy=MachinePolicy(POL, cap={"updates.auto": "notify"}), env={},
                 user={"updates.auto": "off"})
    got = r.resolve("updates.auto")
    assert got.value == "off" and got.capped == ""


def test_a_lock_on_the_ceiling_row_is_a_cap_and_the_user_may_still_go_lower():
    r = Resolver(policy=MachinePolicy(POL, lock={"updates.auto": "notify"}), env={},
                 user={"updates.auto": "stage"})
    got = r.resolve("updates.auto")
    assert got.value == "notify" and got.capped and not got.locked
    got = r.set("updates.auto", "off")            # a lower mode: not refused, not capped
    assert got.value == "off" and not got.capped
    got = r.set("updates.auto", "stage")          # a higher one: stored, then capped
    assert got.value == "notify" and got.capped and r.layer.values["updates.auto"] == "stage"


def test_todays_three_policy_keys_keep_their_meaning():
    pol = parse_machine_policy('self_update = "notify"\nchannel = "stable"\n'
                               'check_interval = "12h"\n', POL)
    r = Resolver(policy=pol, env={"HARNESS_MANAGER_UPDATE_CHANNEL": "beta"},
                 user={"updates.auto": "stage", "updates.channel": "dev"})
    auto, chan, ivl = (r.resolve(k) for k in ("updates.auto", "updates.channel",
                                              "updates.check_interval"))
    assert auto.value == "notify" and auto.capped
    assert chan.value == "stable" and chan.locked
    assert ivl.value == 43200 and ivl.locked


def test_negative_twin_without_the_policy_the_user_and_env_decide():
    r = Resolver(env={"HARNESS_MANAGER_UPDATE_CHANNEL": "beta"},
                 user={"updates.auto": "stage", "updates.channel": "dev"})
    assert r.resolve("updates.auto").value == "stage"
    assert r.resolve("updates.channel").value == "dev"
    assert r.resolve("updates.check_interval").value == 6 * 3600


# --- bad values are skipped, not fatal ---------------------------------------------------------


@pytest.mark.parametrize("layer, raw", [("env", "twelve"), ("user", "23500"),
                                        ("machine", 80)])
def test_a_bad_value_in_a_layer_is_skipped_with_a_problem(layer, raw):
    key = "debug.xvc_port_base"
    r = Resolver(policy=MachinePolicy(POL, default={key: raw} if layer == "machine" else {}),
                 env={"HARNESS_MANAGER_XVC_PORT_BASE": raw} if layer == "env" else {},
                 user={key: raw} if layer == "user" else {},
                 pack_defaults={key: 23700})
    got = r.resolve(key)
    assert got.source == "pack" and got.value == 23700 and len(got.problems) == 1
    assert "ignored" in got.problems[0] and got.apply == "restart"


def test_negative_twin_the_same_layers_with_good_values_win():
    key = "debug.xvc_port_base"
    r = Resolver(env={"HARNESS_MANAGER_XVC_PORT_BASE": "23500"}, user={key: 23600},
                 pack_defaults={key: 23700})
    got = r.resolve(key)
    assert got.source == "env" and got.value == 23500 and not got.problems


def test_set_validates_and_is_all_or_nothing():
    r = Resolver(env={}, user={})
    with pytest.raises(UsageError, match="port"):
        r.set_many({"tools.openocd": "/a", "debug.port_base": 80})
    assert "tools.openocd" not in r.layer.values                 # nothing was stored
    r.set_many({"tools.openocd": "/a", "debug.port_base": 23400})
    assert r.resolve("debug.port_base").value == 23400
    assert r.set("debug.port_base", "23410", text=True).value == 23410   # CLI text parses


def test_read_only_and_secret_rows_are_not_set_through_set():
    r = Resolver(env={}, user={})
    with pytest.raises(UsageError, match="set-secret"):
        r.set("hubs.lab.token", "x")
    with pytest.raises(UsageError, match="HARNESS_MANAGER_STATE_DIR"):
        r.set("advanced.state_dir", "/elsewhere")


# --- boards, hubs, packs, listings -------------------------------------------------------------


def test_a_boards_own_table_then_boards_defaults_then_the_default():
    b = 'boards."mps3@1.2.3.4:6900".power.timeout_s'
    r = Resolver(env={}, user={"boards.defaults.power.timeout_s": 5.0})
    got = r.resolve(b)
    assert got.value == 5.0 and "[boards.defaults]" in got.where
    r = Resolver(env={}, user={"boards.defaults.power.timeout_s": 5.0, b: 7.0})
    assert r.resolve(b).value == 7.0
    assert Resolver(env={}, user={}).resolve(b).value == 3.0     # twin: the built-in


def test_listing_expands_hubs_and_boards_and_keeps_dev_rows_out():
    pol = parse_machine_policy('[hubs.lab]\nhost = "hub.example"\n', POL)
    r = Resolver(policy=pol, env={}, user={"hubs.remote.url": "https://h:7246",
                                           'boards."mps3@x".name': "mps3-01",
                                           "boards.defaults.power.timeout_s": 5.0})
    assert r.instances() == {"hubs": ["lab", "remote"], "boards": ["mps3@x"]}
    keys = [x.key for x in r.listing()]
    assert "hubs.lab.host" in keys and "hubs.remote.url" in keys
    assert 'boards."mps3@x".name' in keys and not any("defaults" in k for k in keys)
    assert "dev.no_daemon" not in keys
    assert "dev.no_daemon" in [x.key for x in r.listing(include_dev=True)]
    lab = r.resolve("hubs.lab.host")
    assert lab.locked and lab.value == "hub.example"            # a machine hub
    assert [x.key for x in r.listing(section="Tools")][0] == "tools.openocd"


def test_a_pack_default_sits_below_the_machine_default():
    r = Resolver(policy=MachinePolicy(POL, default={"harness.source": "/lab/m"}), env={},
                 user={}, pack_defaults={"harness.source": "/pack/m"})
    assert r.resolve("harness.source").source == "machine"
    assert Resolver(env={}, user={}, pack_defaults={"harness.source": "/pack/m"}).resolve(
        "harness.source").source == "pack"


def test_negative_twin_a_machine_default_on_a_user_row_is_ignored_with_a_problem():
    r = Resolver(policy=MachinePolicy(POL, default={"kits.jobs": 4}), env={}, user={},
                 pack_defaults={"kits.jobs": 8})
    assert r.resolve("kits.jobs").source == "pack"
    assert any("[default] kits.jobs" in p for p in r.problems)


# --- secrets resolve to their status only ------------------------------------------------------


def test_a_secret_resolves_to_set_or_not_never_its_value(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("hubs.lab.token", "tok-NEVER-SHOWN-1")
    r = Resolver(env={"HARNESS_MANAGER_GITHUB_TOKEN": "ghp_NEVER_SHOWN_2"},
                 user={"hubs.lab.url": "https://h:7246"}, secrets=store)
    views = json.dumps([x.view() for x in r.listing()])
    assert "NEVER_SHOWN" not in views and "NEVER-SHOWN" not in views
    tok = r.resolve("hubs.lab.token")
    assert tok.value == {"set": True} and tok.secret["backend"] == "file"
    gh = r.resolve("updates.github_token")
    assert gh.source == "env" and gh.secret["where"] == "$HARNESS_MANAGER_GITHUB_TOKEN"


def test_negative_twin_an_unset_secret_says_not_set():
    got = Resolver(env={}, user={}, secrets=None).resolve("hubs.lab.token")
    assert got.value == {"set": False} and got.source == "default"


def test_a_secret_pasted_into_settings_toml_is_ignored_and_never_echoed(tmp_path):
    r = Resolver(env={}, user={"hubs.lab.token": "tok-PASTED-VALUE"},
                 secrets=SecretStore(tmp_path, keyrings=[]))
    got = r.resolve("hubs.lab.token")
    assert got.value == {"set": False} and "reference" in got.problems[0]
    assert "PASTED" not in json.dumps(got.view())
    ok = Resolver(env={}, user={"hubs.lab.token": "file:~/hub.token"},
                  secrets=SecretStore(tmp_path, keyrings=[])).resolve("hubs.lab.token")
    assert ok.value == {"set": True} and ok.secret["backend"] == "file" and not ok.problems


def test_file_backend_is_what_an_unreachable_keyring_leaves(tmp_path):
    # (the full keyring behaviour is in test_settings_secrets)
    assert FileBackend(tmp_path).available() == (True, "")


def test_an_unknown_key_in_settings_toml_is_a_problem_not_a_failure():
    r = Resolver(env={}, user={"tools.opencd": "/typo", "tools.openocd": "/o"})
    assert r.problems == ["settings.toml: tools.opencd is not a setting; ignored"]
    assert r.resolve("tools.openocd").value == "/o"


def test_negative_twin_known_keys_raise_no_problem():
    assert Resolver(env={}, user={"tools.openocd": "/o", "hubs.lab.url": ""}).problems == []


def test_a_legacy_inline_plug_password_in_boards_toml_is_set_inline_never_shown(tmp_path):
    from harness_manager.settings import SettingsFiles

    (tmp_path / "boards.toml").write_text(
        '[boards.lab.power]\nkind = "shelly_gen2"\nurl = "http://plug"\n'
        'auth = { user = "admin", password = "pw-INLINE-VALUE" }\n')
    r = Resolver(env={}, user=SettingsFiles(tmp_path).read(),
                 secrets=SecretStore(tmp_path, keyrings=[]))
    got = r.resolve("boards.lab.power.auth.password")
    assert got.value == {"set": True} and got.secret["backend"] == "inline"
    assert "set-secret" in got.problems[0] and "INLINE" not in json.dumps(got.view())
    # twin: the same line in settings.toml is a pasted secret: ignored, not set
    r = Resolver(env={}, user={"hubs.lab.token": "pw-INLINE-VALUE"},
                 secrets=SecretStore(tmp_path, keyrings=[]))
    assert r.resolve("hubs.lab.token").value == {"set": False}
