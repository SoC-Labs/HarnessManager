"""SET-CORE: the admin policy's [lock], [default], [hubs.*] and today's three keys (david S3).

The three U6 keys keep their meaning, and the updater (``services.update.policy``) and the
settings resolver read them from one parser, so they cannot disagree (the matrix below).
"""

from __future__ import annotations

import pytest

from harness_manager.services.update.policy import load_policy, parse_policy
from harness_manager.settings import Resolver, load_machine_policy, parse_machine_policy

POL = "/etc/harness-manager/policy.toml"


def test_the_three_legacy_keys_become_a_cap_and_two_locks():
    pol = parse_machine_policy('self_update = "notify"\nchannel = "stable"\n'
                               'check_interval = "12h"\n', POL)
    assert pol.cap == {"updates.auto": "notify"}
    assert pol.lock == {"updates.channel": "stable", "updates.check_interval": 43200}
    assert pol.problems == ()


def test_negative_twin_no_legacy_keys_no_locks_and_no_cap():
    pol = parse_machine_policy("", POL)
    assert pol.cap == {} and pol.lock == {} and pol.default == {}


@pytest.mark.parametrize("spelling", [
    '[lock]\nupdates.channel = "beta"\n',
    '[lock]\n"updates.channel" = "beta"\n',
    '[lock.updates]\nchannel = "beta"\n',
])
def test_a_lock_may_name_the_update_channel_by_its_settings_key(spelling):
    u6 = parse_policy(spelling, POL)
    assert u6.channel == "beta" and u6.problems == ()        # the updater honours it
    pol = parse_machine_policy(spelling, POL)
    assert pol.lock["updates.channel"] == "beta"             # and so does the menu


def test_negative_twin_before_set_core_the_lock_table_was_an_unknown_key():
    # A [lock] with no update key leaves the updater's view exactly as it was, silently.
    u6 = parse_policy('[lock]\ntools.vivado = "/v"\n[default]\nkits.jobs = 4\n', POL)
    assert u6.channel == "" and u6.self_update == "stage" and u6.problems == ()
    assert parse_policy('colour = "blue"\n', POL).problems == ("unknown key 'colour' is ignored",)


def test_a_top_level_key_wins_over_a_lock_that_disagrees_with_a_warning():
    text = 'channel = "stable"\n[lock]\nupdates.channel = "beta"\n'
    u6 = parse_policy(text, POL)
    assert u6.channel == "stable" and "disagrees" in u6.problems[0]
    assert parse_machine_policy(text, POL).lock["updates.channel"] == "stable"


@pytest.mark.parametrize("text", [
    "",
    'self_update = "off"\n',
    'self_update = false\n',
    'self_update = true\n',
    'self_update = "sometimes"\n',
    'channel = "Beta Channel"\n',
    'channel = "beta"\ncheck_interval = 5\n',
    'check_interval = "soon"\n',
    'check_interval = 0\n',
    '[lock]\nupdates.auto = "notify"\n',
    '[lock]\nupdates.check_interval = "2h"\n',
    '[lock]\nupdates.channel = "dev"\nupdates.auto = "off"\n',
    "self_update = \n",
])
@pytest.mark.parametrize("user_auto, user_channel", [("stage", "dev"), ("off", "")])
def test_the_updater_and_the_menu_agree_on_every_policy(text, user_auto, user_channel):
    """For each policy file, U6's ``effective`` inputs equal what the resolver shows."""
    u6 = parse_policy(text, POL)
    user = {"updates.auto": user_auto, **({"updates.channel": user_channel}
                                          if user_channel else {})}
    r = Resolver(policy=parse_machine_policy(text, POL), env={}, user=user)
    auto = r.resolve("updates.auto").value
    order = ("off", "notify", "stage")
    want_auto = min(u6.self_update, user_auto, key=order.index)
    assert auto == want_auto
    chan = r.resolve("updates.channel")
    if u6.channel:
        assert chan.locked and chan.value == u6.channel
    elif u6.self_update == "off" and u6.why_off and "channel" in u6.why_off:
        assert chan.locked and chan.value == "stable" and chan.problems   # fail closed
    else:
        assert chan.value == (user_channel or "stable")
    assert r.resolve("updates.check_interval").value == u6.interval_s


def test_lock_default_and_machine_hubs_are_read():
    pol = parse_machine_policy(
        '[lock]\ntools.vivado = "/tools/vivado"\n"boards.*.power.cycle" = false\n'
        '[default]\nupdates.mirrors = ["/lab/mirror"]\n'
        '[hubs.lab]\ntransport = "ssh"\nhost = "hub.example"\n', POL)
    assert pol.lock["tools.vivado"] == "/tools/vivado"
    assert pol.lock["boards.*.power.cycle"] is False
    assert pol.default == {"updates.mirrors": ["/lab/mirror"]}
    assert pol.hubs == {"lab": {"transport": "ssh", "host": "hub.example"}}
    assert pol.lock["hubs.lab.host"] == "hub.example"


def test_a_token_in_a_machine_hub_is_dropped_with_the_way_out():
    pol = parse_machine_policy('[hubs.lab]\nhost = "h"\ntoken = "tok-IN-POLICY"\n', POL)
    assert "token" not in pol.hubs["lab"] and "hubs.lab.token" not in pol.lock
    assert "set-secret hubs.lab.token" in pol.problems[0]
    assert "tok-IN-POLICY" not in " ".join(pol.problems)


def test_negative_twin_a_machine_hub_without_a_token_has_no_problem():
    assert parse_machine_policy('[hubs.lab]\nhost = "h"\n', POL).problems == ()


def test_unknown_or_unlockable_keys_are_problems_not_failures():
    pol = parse_machine_policy('[lock]\ntools.nothing = 1\ngeneral.theme = "dark"\n'
                               'updates.github_token = "x"\n[default]\nkits.jobs = 4\n', POL)
    r = Resolver(policy=pol, env={}, user={})
    text = "\n".join(r.problems)
    assert "tools.nothing is not a setting" in text
    assert "general.theme is not the administrator's" in text
    assert "updates.github_token is not the administrator's" in text and "secret" in text
    assert "[default] kits.jobs" in text
    assert r.resolve("general.theme").source == "default"


def test_a_lock_or_default_that_is_not_a_table_is_a_problem():
    pol = parse_machine_policy('lock = 3\ndefault = "x"\nhubs = 1\n', POL)
    assert pol.lock == {} and pol.default == {} and len(pol.problems) == 3


def test_an_unparsable_policy_fails_closed_self_update_off():
    pol = parse_machine_policy("self_update = \n", POL)
    assert pol.cap == {"updates.auto": "off"} and pol.problems
    assert Resolver(policy=pol, env={}, user={"updates.auto": "stage"}).resolve(
        "updates.auto").value == "off"


def test_the_files_on_disk(tmp_path):
    assert load_machine_policy(tmp_path / "absent.toml").path == ""
    d = tmp_path / "dir.toml"
    d.mkdir()
    pol = load_machine_policy(d)
    assert pol.cap == {"updates.auto": "off"} and "cannot be read" in pol.problems[0]
    p = tmp_path / "policy.toml"
    p.write_text('[lock]\nupdates.channel = "beta"\n')
    assert load_machine_policy(p).lock == {"updates.channel": "beta"}
    assert load_policy(p).channel == "beta"
