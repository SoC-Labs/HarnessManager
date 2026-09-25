"""SET-CORE: settings.toml and boards.toml on disk (david S1): tomlkit edits in place, 0600,
the settings.json migration, and the updater's delegation to them (CCR SET-CORE-3).
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from harness_manager.core.errors import ExitCode, HeldError, UsageError
from harness_manager.services.update import selfupdate as su
from harness_manager.services.update.policy import Policy
from harness_manager.settings import Resolver, SettingsFiles, config_dir

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_config_dir_is_the_callers_state_dir_then_the_variable_then_home(tmp_path):
    assert config_dir(tmp_path / "svc", {"HARNESS_MANAGER_STATE_DIR": "/x"}) == tmp_path / "svc"
    assert str(config_dir(None, {"HARNESS_MANAGER_STATE_DIR": "/x"})) == "/x"
    assert config_dir(None, {}).parts[-2:] == (".config", "harness-manager")


@posix_only
def test_the_user_file_round_trips_at_0600(tmp_path):
    files = SettingsFiles(tmp_path)
    flat = {"updates.channel": "beta", "hubs.lab.url": "https://hub.example:7246",
            "hubs.lab.transport": "rest", "updates.mirrors": ["/a", "/b"],
            "debug.xvc_port_base": 23500, "hubs.lab.insecure": False}
    files.write(flat)
    back = files.read()
    assert {k: back.values[k] for k in flat} == flat and back.hubs == ["lab"]
    assert mode(files.settings_path) == 0o600 and back.migrated


@posix_only
def test_negative_twin_a_loose_umask_still_gives_0600(tmp_path):
    old = os.umask(0)
    try:
        SettingsFiles(tmp_path).write({"tools.openocd": "/o"})
    finally:
        os.umask(old)
    assert mode(tmp_path / "settings.toml") == 0o600


def test_hand_written_comments_survive_a_set_and_an_unset(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("# my lab machine\nschema = 1\n\n[tools]\n"
                 "openocd = \"/opt/openocd\"   # the patched build\n"
                 "vivado = \"off\"\n\n# the lab hub\n[hubs.lab]\nhost = \"hub.example\"\n")
    files = SettingsFiles(tmp_path)
    files.write({"tools.hw_server": "/opt/hw_server", "hubs.lab.group": "fpga"})
    files.write({"tools.vivado": None})
    text = p.read_text()
    for kept in ("# my lab machine", "# the patched build", "# the lab hub",
                 'hw_server = "/opt/hw_server"', 'group = "fpga"'):
        assert kept in text, kept
    assert "vivado" not in text
    assert files.read().values["tools.openocd"] == "/opt/openocd"


def test_unset_prunes_a_table_it_emptied_but_not_one_with_a_comment(tmp_path):
    files = SettingsFiles(tmp_path)
    files.write({"kits.jobs": 4, "hubs.lab.host": "h"})
    files.write({"kits.jobs": None, "hubs.lab.host": None})
    text = (tmp_path / "settings.toml").read_text()
    assert "[kits]" not in text and "[hubs" not in text
    (tmp_path / "settings.toml").write_text("schema = 1\n[kits]\n# keep me\njobs = 4\n")
    files.write({"kits.jobs": None})
    assert "# keep me" in (tmp_path / "settings.toml").read_text()


# --- the settings.json migration ---------------------------------------------------------------


def _legacy(tmp_path, **rec):
    p = tmp_path / "update" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))
    return p


def test_before_any_write_update_settings_json_is_read_as_the_fallback(tmp_path):
    _legacy(tmp_path, channel="beta", auto="notify")
    layer = SettingsFiles(tmp_path).read()
    assert layer.values["updates.channel"] == "beta" and not layer.migrated
    assert layer.origin["updates.auto"] == "update/settings.json"
    assert not (tmp_path / "settings.toml").exists()            # nothing moved on a read


def test_the_first_write_folds_it_into_updates_and_records_the_schema(tmp_path):
    _legacy(tmp_path, channel="beta", auto="notify")
    SettingsFiles(tmp_path).write({"tools.openocd": "/o"})       # any first write
    text = (tmp_path / "settings.toml").read_text()
    assert "schema = 1" in text and 'channel = "beta"' in text and 'auto = "notify"' in text
    assert (tmp_path / "update" / "settings.json").exists()     # left for a rollback


def test_negative_twin_after_the_migration_settings_json_is_no_longer_read(tmp_path):
    files = SettingsFiles(tmp_path)
    files.write({"tools.openocd": "/o"})                         # migrated, nothing to fold
    _legacy(tmp_path, channel="beta")                            # an old version wrote it
    assert "updates.channel" not in files.read().values


def test_a_change_being_written_is_not_overwritten_by_the_fold(tmp_path):
    _legacy(tmp_path, channel="beta", auto="notify")
    SettingsFiles(tmp_path).write({"updates.channel": None, "updates.auto": "off"})
    values = SettingsFiles(tmp_path).read().values
    assert "updates.channel" not in values and values["updates.auto"] == "off"


def test_an_update_write_keeps_settings_json_current_for_a_rollback(tmp_path):
    files = SettingsFiles(tmp_path)
    files.write({"updates.auto": "off"})
    assert json.loads((tmp_path / "update" / "settings.json").read_text()) == \
        {"channel": "", "auto": "off"}
    files.write({"updates.channel": "dev"})
    assert json.loads((tmp_path / "update" / "settings.json").read_text()) == \
        {"channel": "dev", "auto": "off"}


def test_negative_twin_other_writes_leave_settings_json_alone(tmp_path):
    SettingsFiles(tmp_path).write({"tools.openocd": "/o"})
    assert not (tmp_path / "update" / "settings.json").exists()


# --- a broken file ----------------------------------------------------------------------------


def test_a_broken_file_reads_as_empty_with_a_problem_and_is_never_overwritten(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[tools\nopenocd = \n")
    files = SettingsFiles(tmp_path)
    layer = files.read()
    assert not layer.values and "not valid TOML" in layer.problems[0]
    with pytest.raises(UsageError, match="not overwritten") as exc:
        files.write({"tools.openocd": "/o"})
    assert exc.value.code == ExitCode.USAGE and p.read_text() == "[tools\nopenocd = \n"
    r = Resolver(user=layer, env={})                         # the menu still works
    assert r.resolve("tools.openocd").source == "default" and r.problems


def test_a_value_where_a_table_is_needed_is_refused(tmp_path):
    (tmp_path / "settings.toml").write_text('schema = 1\nhubs = "oops"\n')
    with pytest.raises(UsageError, match="not a table"):
        SettingsFiles(tmp_path).write({"hubs.lab.host": "h"})


# --- boards.toml -------------------------------------------------------------------------------


@posix_only
def test_board_keys_go_to_boards_toml_keeping_comments_mode_and_a_backup(tmp_path):
    p = tmp_path / "boards.toml"
    original = ('# the lab board\n[boards."mps3@192.168.10.101:6900"]\n'
                'match = ["192.168.10.101"]\nhub = { host = "hub.example", '
                'target = "mps3_01_pl" }   # the inline hub\n')
    p.write_text(original)
    os.chmod(p, 0o640)
    files = SettingsFiles(tmp_path)
    files.write({'boards."mps3@192.168.10.101:6900".name': "mps3-01",
                 'boards."mps3@192.168.10.101:6900".hub.use': "lab"})
    text = p.read_text()
    assert "# the lab board" in text and "# the inline hub" in text
    assert 'name = "mps3-01"' in text and 'use = "lab"' in text
    assert mode(p) == 0o640                                     # its mode is its owner's
    backups = list(tmp_path.glob("boards.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    values = files.read().values
    assert values['boards."mps3@192.168.10.101:6900".hub.target'] == "mps3_01_pl"
    assert values['boards."mps3@192.168.10.101:6900".hub.use'] == "lab"
    assert not (tmp_path / "settings.toml").exists()            # board keys never go there


@posix_only
def test_negative_twin_a_new_boards_toml_is_0600_with_no_backup(tmp_path):
    SettingsFiles(tmp_path).write({'boards."mps3@x".name': "a"})
    assert mode(tmp_path / "boards.toml") == 0o600
    assert not list(tmp_path.glob("boards.toml.bak-*"))


def test_boards_in_settings_toml_are_ignored_with_a_problem(tmp_path):
    (tmp_path / "settings.toml").write_text('schema = 1\n[boards.x]\nname = "a"\n')
    layer = SettingsFiles(tmp_path).read()
    assert "boards.x.name" not in layer.values and "belongs in boards.toml" in layer.problems[0]


# --- one writer at a time ----------------------------------------------------------------------


def test_a_second_writer_waits_for_the_lock_then_gives_up_as_held(tmp_path):
    files = SettingsFiles(tmp_path)
    with files.lock():
        with pytest.raises(HeldError), SettingsFiles(tmp_path).lock(timeout_s=0.2):
            pass


def test_negative_twin_a_writer_that_waits_gets_it_when_released(tmp_path):
    files = SettingsFiles(tmp_path)
    got = []

    def other():
        with SettingsFiles(tmp_path).lock(timeout_s=5):
            got.append(time.monotonic())

    with files.lock():
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.2)
        released = time.monotonic()
    t.join(5)
    assert got and got[0] >= released


# --- the updater's settings go through the store (CCR SET-CORE-3) ------------------------------


def test_the_updater_saves_to_settings_toml_and_reads_it_back(tmp_path):
    su.save_settings(tmp_path, channel="beta", auto="notify")
    text = (tmp_path / "settings.toml").read_text()
    assert "[updates]" in text and 'channel = "beta"' in text
    assert su.load_settings(tmp_path) == su.Settings(channel="beta", auto="notify")
    su.save_settings(tmp_path, channel="")                       # back to the default
    assert su.load_settings(tmp_path) == su.Settings(channel="", auto="notify")
    assert json.loads(su.settings_path(tmp_path).read_text()) == \
        {"channel": "", "auto": "notify"}                         # the rollback copy


def test_negative_twin_the_updater_still_reads_an_unmigrated_settings_json(tmp_path):
    _legacy(tmp_path, channel="dev", auto="off")
    assert su.load_settings(tmp_path) == su.Settings(channel="dev", auto="off")
    _legacy(tmp_path, channel="Not A Channel", auto="always")      # bad values: unset
    assert su.load_settings(tmp_path) == su.Settings()


def test_the_updater_refuses_before_writing_anything(tmp_path):
    pinned = Policy(path="/etc/p.toml", channel="stable")
    with pytest.raises(Exception, match="pins"):
        su.save_settings(tmp_path, channel="beta", policy=pinned)
    assert not (tmp_path / "settings.toml").exists()


def test_the_resolver_and_the_updater_see_the_same_user_choice(tmp_path):
    su.save_settings(tmp_path, auto="off")
    r = Resolver.load(tmp_path, env={}, policy_path=tmp_path / "no-policy.toml")
    got = r.resolve("updates.auto")
    assert got.value == "off" and got.source == "user" and got.where == "settings.toml"
    r.set("updates.auto", "notify")
    assert su.load_settings(tmp_path).auto == "notify"
