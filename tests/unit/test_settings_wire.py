"""SET-WIRE: every reader that took a variable now takes the setting (``settings.runtime``).

For each switched reader: a value in ``settings.toml`` now takes effect where only the
variable worked before, and the variable still wins where the precedence says it does
(the negative twin). Then the precedence exceptions (``updates.channel``), the admin's lock,
a bad value, what ``live`` and ``restart`` mean, the one state-dir rule and the service's own
config dir, ``[boards.defaults]``, and the ``daemon start`` flags.

Every file is under the test's own ``$HARNESS_MANAGER_STATE_DIR`` (``tests/conftest.py``);
the keyring is off; the policy file is the test's (``runtime.POLICY_PATH``).
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.settings import runtime
from harness_manager.settings.files import config_dir, use_config_dir


@pytest.fixture(autouse=True)
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime.reset()
    monkeypatch.setattr(runtime, "POLICY_PATH", tmp_path / "policy.toml")
    prev = use_config_dir(None)
    yield
    use_config_dir(prev)
    runtime.reset()


def state() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def write_settings(text: str, root: Path | None = None) -> Path:
    root = root or state()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def write_policy(tmp_path: Path, text: str) -> None:
    (tmp_path / "policy.toml").write_text(text, encoding="utf-8")


def exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


# --- tools.openocd (services/debug.py) ------------------------------------------------------------


def test_openocd_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager.services import debug

    monkeypatch.delenv(debug.OPENOCD_ENV, raising=False)
    mine = exe(tmp_path / "bin" / "my-openocd")
    write_settings(f'[tools]\nopenocd = "{mine}"\n')
    assert debug.find_openocd() == str(mine)


def test_negative_twin_the_openocd_variable_still_wins(tmp_path, monkeypatch):
    from harness_manager.services import debug

    write_settings(f'[tools]\nopenocd = "{exe(tmp_path / "file-openocd")}"\n')
    env_one = exe(tmp_path / "env-openocd")
    monkeypatch.setenv(debug.OPENOCD_ENV, str(env_one))
    assert debug.find_openocd() == str(env_one)


def test_a_missing_openocd_names_where_it_was_set(tmp_path, monkeypatch):
    from harness_manager.core.errors import UnavailableError
    from harness_manager.services import debug

    monkeypatch.delenv(debug.OPENOCD_ENV, raising=False)
    write_settings(f'[tools]\nopenocd = "{tmp_path / "nope"}"\n')
    with pytest.raises(UnavailableError) as exc:
        debug.find_openocd()
    assert f"tools.openocd={tmp_path / 'nope'} (settings.toml) does not exist" in exc.value.reason
    monkeypatch.setenv(debug.OPENOCD_ENV, str(tmp_path / "gone"))       # twin: today's words
    with pytest.raises(UnavailableError) as exc:
        debug.find_openocd()
    assert f"HARNESS_MANAGER_OPENOCD={tmp_path / 'gone'} does not exist" in exc.value.reason


# --- debug.port_base / debug.xvc_port_base (restart rows) ----------------------------------------


def test_the_debug_port_bases_come_from_the_settings(monkeypatch):
    from harness_manager.services.debug import DebugService
    from harness_manager.services.xvc import XvcService

    monkeypatch.delenv("HARNESS_MANAGER_DEBUG_PORT_BASE", raising=False)
    monkeypatch.delenv("HARNESS_MANAGER_XVC_PORT_BASE", raising=False)
    write_settings("[debug]\nport_base = 23400\nxvc_port_base = 23650\n")
    assert DebugService(None)._pinned_base() == 23400
    assert XvcService(None)._pinned_base() == 23650


def test_negative_twin_the_port_base_variables_win_and_zero_is_automatic(monkeypatch):
    from harness_manager.services.debug import DebugService
    from harness_manager.services.xvc import XvcService

    write_settings("[debug]\nport_base = 23400\nxvc_port_base = 23650\n")
    monkeypatch.setenv("HARNESS_MANAGER_DEBUG_PORT_BASE", "23500")
    monkeypatch.setenv("HARNESS_MANAGER_XVC_PORT_BASE", "23700")
    assert DebugService(None)._pinned_base() == 23500
    assert XvcService(None)._pinned_base() == 23700
    monkeypatch.setenv("HARNESS_MANAGER_DEBUG_PORT_BASE", "0")        # the row's "automatic"
    assert DebugService(None)._pinned_base() is None
    assert DebugService(None, port_base=23800)._pinned_base() == 23800  # the kwarg first


def test_a_restart_row_keeps_the_value_the_process_started_with(monkeypatch):
    from harness_manager.services.debug import DebugService

    monkeypatch.delenv("HARNESS_MANAGER_DEBUG_PORT_BASE", raising=False)
    write_settings("[debug]\nport_base = 23400\n")
    assert DebugService(None)._pinned_base() == 23400
    write_settings("[debug]\nport_base = 23460\n")
    runtime.refresh()                                        # settings.changed: still not
    assert DebugService(None)._pinned_base() == 23400        # a restart row: next start
    runtime.reset()                                          # a new process
    assert DebugService(None)._pinned_base() == 23460


def test_negative_twin_a_live_row_applies_at_its_next_use(tmp_path, monkeypatch):
    from harness_manager.services import debug

    monkeypatch.delenv(debug.OPENOCD_ENV, raising=False)
    a, b = exe(tmp_path / "a" / "openocd"), exe(tmp_path / "b" / "openocd")
    write_settings(f'[tools]\nopenocd = "{a}"\n')
    assert debug.find_openocd() == str(a)
    write_settings(f'[tools]\nopenocd = "{b}"\n')           # same size, no refresh()
    assert debug.find_openocd() == str(b)


# --- tools.hw_server (services/xvc.py) -----------------------------------------------------------


def test_hw_server_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager.services import xvc

    monkeypatch.delenv(xvc.HW_SERVER_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    mine = exe(tmp_path / "hw" / "hw_server")
    write_settings(f'[tools]\nhw_server = "{mine}"\n')
    assert xvc.find_hw_server() == str(mine)


def test_negative_twin_the_hw_server_variable_still_wins(tmp_path, monkeypatch):
    from harness_manager.services import xvc

    write_settings(f'[tools]\nhw_server = "{exe(tmp_path / "file" / "hw_server")}"\n')
    env_one = exe(tmp_path / "env" / "hw_server")
    monkeypatch.setenv(xvc.HW_SERVER_ENV, str(env_one))
    assert xvc.find_hw_server() == str(env_one)


# --- tools.vivado (services/kit/vivado.py) -------------------------------------------------------


def test_vivado_from_the_settings_file(monkeypatch):
    from harness_manager.services.kit import vivado

    monkeypatch.delenv(vivado.ENV, raising=False)            # conftest turns it off
    write_settings('[tools]\nvivado = "off"\n')
    found = vivado.discover(roots=())
    assert found.disabled and "tools.vivado (settings.toml)=off" in found.reason


def test_negative_twin_the_vivado_variable_still_wins(tmp_path, monkeypatch):
    from harness_manager.services.kit import vivado

    write_settings('[tools]\nvivado = "off"\n')
    monkeypatch.setenv(vivado.ENV, "none")
    found = vivado.discover(roots=())
    assert found.disabled and found.reason == "Vivado discovery is off ($HARNESS_MANAGER_VIVADO=none)"
    # a caller's own environment is its env layer, and the file still sits below it
    found = vivado.discover(env={}, which=lambda _: None, roots=())
    assert found.disabled and "tools.vivado" in found.reason


# --- kits.hub_dir (services/kit/service.py) ------------------------------------------------------


def test_the_kit_hub_dir_from_the_settings_applies_to_a_running_service(tmp_path, monkeypatch):
    from harness_manager.services.kit import KitService

    monkeypatch.delenv("HARNESS_MANAGER_KIT_HUB_DIR", raising=False)
    kits = KitService(object(), tmp_path / "w")
    assert kits.hub.root is None and "kits.hub_dir" in kits.hub.reason
    write_settings(f'[kits]\nhub_dir = "{tmp_path / "mints"}"\n')
    assert kits.hub.root == tmp_path / "mints"               # live: the same service


def test_negative_twin_the_kit_hub_variable_wins_and_a_given_hub_is_final(tmp_path, monkeypatch):
    from harness_manager.services.kit import HubSource, KitService

    write_settings(f'[kits]\nhub_dir = "{tmp_path / "file"}"\n')
    monkeypatch.setenv("HARNESS_MANAGER_KIT_HUB_DIR", str(tmp_path / "env"))
    assert KitService(object(), tmp_path / "w").hub.root == tmp_path / "env"
    given = HubSource(tmp_path / "given")
    assert KitService(object(), tmp_path / "w", hub=given).hub is given


# --- updates.source, updates.mirrors, updates.channel (services/update) -------------------------


def test_the_update_source_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager.services.update.channel import channel_url

    monkeypatch.delenv("HARNESS_MANAGER_UPDATE_SOURCE", raising=False)
    (tmp_path / "mirror").mkdir()
    write_settings(f'[updates]\nsource = "{tmp_path / "mirror"}"\n')
    assert channel_url(None, "stable") == (tmp_path / "mirror" / "channel.json").as_uri()


def test_negative_twin_the_source_variable_and_an_explicit_source_win(tmp_path, monkeypatch):
    from harness_manager.services.update.channel import channel_url

    write_settings(f'[updates]\nsource = "{tmp_path / "file"}"\n')
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", "https://env.example/{channel}/")
    assert channel_url(None, "beta") == "https://env.example/beta/channel.json"
    assert channel_url("https://given.example/", "beta") == "https://given.example/channel.json"


def test_a_bad_source_variable_fails_instead_of_falling_back_to_github(monkeypatch):
    from harness_manager.services.update.channel import channel_url

    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", "ftp://old.example/")
    with pytest.raises(UsageError) as exc:
        channel_url(None, "stable")
    assert "HARNESS_MANAGER_UPDATE_SOURCE" in exc.value.message + exc.value.hint


def test_negative_twin_a_bad_source_in_the_file_is_skipped_with_a_warning(monkeypatch, caplog):
    from harness_manager.services.update.channel import channel_url

    monkeypatch.delenv("HARNESS_MANAGER_UPDATE_SOURCE", raising=False)
    write_settings('[updates]\nsource = "ftp://old.example/"\n')
    with caplog.at_level(logging.WARNING, logger="harness_manager.settings.runtime"):
        assert channel_url(None, "stable").startswith("https://github.com/SoC-Labs/")
    assert "updates.source" in caplog.text


def test_the_mirrors_come_from_the_settings_at_each_download(tmp_path, monkeypatch):
    from harness_manager.services.update.download import Downloader, mirrors_from_env

    monkeypatch.delenv("HARNESS_MANAGER_UPDATE_MIRRORS", raising=False)
    dl = Downloader(tmp_path / "cache")
    assert dl.mirror_roots() == []
    write_settings('[updates]\nmirrors = ["/lab/a", "/lab/b"]\n')
    assert mirrors_from_env() == ("/lab/a", "/lab/b")
    assert dl.mirror_roots() == ["/lab/a", "/lab/b"]          # live: the same downloader


def test_negative_twin_the_mirrors_variable_wins_and_given_mirrors_are_final(tmp_path, monkeypatch):
    from harness_manager.services.update.download import Downloader

    write_settings('[updates]\nmirrors = ["/lab/a"]\n')
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_MIRRORS", "/env/x, /env/y")
    assert Downloader(tmp_path / "c").mirror_roots() == ["/env/x", "/env/y"]
    assert Downloader(tmp_path / "d", mirrors=()).mirror_roots() == []


def test_the_default_channel_keeps_its_variable_only_meaning(monkeypatch):
    """``updates.channel`` in the file is the APP's channel (the Updates card, read by
    ``selfupdate.effective``); the harness catalogues' default stays the variable's."""
    from harness_manager.services.update.channel import default_channel

    monkeypatch.delenv("HARNESS_MANAGER_UPDATE_CHANNEL", raising=False)
    write_settings('[updates]\nchannel = "beta"\n')
    assert default_channel() == "stable"
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_CHANNEL", "dev")      # twin: the variable
    assert default_channel() == "dev"


def test_the_app_channel_keeps_the_user_above_the_variable(monkeypatch):
    """The precedence exception is kept: ``env_rank="under-user"``."""
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_CHANNEL", "dev")
    assert runtime.value("updates.channel") == "dev"
    write_settings('[updates]\nchannel = "beta"\n')
    assert runtime.value("updates.channel") == "beta"


# --- updates.github_token (services/update/github.py): env, the store, gh -----------------------


def _gh(value: str):
    import subprocess

    def run(argv):
        return subprocess.CompletedProcess(argv, 0, value + "\n", "")
    return run


def test_a_stored_github_token_is_used_before_gh(monkeypatch):
    from harness_manager.services.update import github
    from harness_manager.settings.secrets import SecretStore

    monkeypatch.delenv(github.TOKEN_ENV, raising=False)
    assert github.resolve_token(runner=_gh("from-gh"), gh="gh") == "from-gh"
    SecretStore(state()).set("updates.github_token", "from-the-store")
    assert github.resolve_token(runner=_gh("from-gh"), gh="gh") == "from-the-store"


def test_negative_twin_the_token_variable_still_wins_over_the_store(monkeypatch):
    from harness_manager.services.update import github
    from harness_manager.settings.secrets import SecretStore

    SecretStore(state()).set("updates.github_token", "from-the-store")
    monkeypatch.setenv(github.TOKEN_ENV, "from-env")
    assert github.resolve_token(runner=_gh("from-gh"), gh="gh") == "from-env"
    assert github.resolve_token({}, runner=_gh("from-gh"), gh="gh") == "from-the-store"


def test_a_token_reference_in_the_file_is_followed(tmp_path, monkeypatch):
    from harness_manager.services.update import github

    monkeypatch.delenv(github.TOKEN_ENV, raising=False)
    monkeypatch.setenv("MY_GH_TOKEN", "from-my-variable")
    write_settings('[updates]\ngithub_token = "env:MY_GH_TOKEN"\n')
    assert github.resolve_token(runner=_gh("from-gh"), gh="gh") == "from-my-variable"
    write_settings('[updates]\ngithub_token = "gh"\n')                   # twin: gh by name
    assert github.resolve_token(runner=_gh("from-gh"), gh="gh") == "from-gh"


def test_the_update_service_asks_again_after_settings_changed(tmp_path, monkeypatch):
    from harness_manager.services.update import UpdateService, github
    from harness_manager.settings.secrets import SecretStore

    monkeypatch.delenv(github.TOKEN_ENV, raising=False)
    monkeypatch.setattr(github, "gh_cli_token", lambda **_: None)
    bus = EventBus()
    svc = UpdateService(state_dir=state(), bus=bus, app_version="0.1.0")
    assert not svc.downloader.has_token()
    SecretStore(state()).set("updates.github_token", "set-later")
    bus.publish(Event("settings.changed", "", {"keys": ["general.theme"]}))
    assert not svc.downloader.has_token()                    # twin: another key, still cached
    bus.publish(Event("settings.changed", "", {"keys": ["updates.github_token"]}))
    assert svc.downloader.has_token()


# --- tools.uv (services/update/app.py) -----------------------------------------------------------


def test_uv_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe

    monkeypatch.delenv("HARNESS_MANAGER_UV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    write_settings(f'[tools]\nuv = "{tmp_path / "my-uv"}"\n')
    up = AppUpdater(AppLayout(tmp_path / "app"), LocalBusyProbe(state()))
    assert up.find_uv() == str(tmp_path / "my-uv")


def test_negative_twin_the_uv_variable_wins_and_the_installers_uv_is_first(tmp_path, monkeypatch):
    from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe

    write_settings(f'[tools]\nuv = "{tmp_path / "file-uv"}"\n')
    monkeypatch.setenv("HARNESS_MANAGER_UV", "/opt/env/uv")
    assert AppUpdater(AppLayout(tmp_path / "a"), LocalBusyProbe(state())).find_uv() == "/opt/env/uv"
    up = AppUpdater(AppLayout(tmp_path / "a"), LocalBusyProbe(state()), uv="/installer/uv")
    assert up.find_uv() == "/installer/uv"


# --- general.app_browser (web/window.py) ---------------------------------------------------------


def test_the_app_browser_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager.web import window

    monkeypatch.delenv(window.ENV_APP_BROWSER, raising=False)
    chrome = exe(tmp_path / "my-chrome")
    write_settings(f'[general]\napp_browser = "{chrome}"\n')
    assert window.find_app_browser(which=lambda _: None) == str(chrome)


def test_negative_twin_the_app_browser_variable_still_wins(tmp_path, monkeypatch):
    from harness_manager.web import window

    write_settings(f'[general]\napp_browser = "{exe(tmp_path / "file-chrome")}"\n')
    env_one = exe(tmp_path / "env-chrome")
    monkeypatch.setenv(window.ENV_APP_BROWSER, str(env_one))
    assert window.find_app_browser(which=lambda _: None) == str(env_one)


# --- mps3.openocd_cfg_dir and mps3.overlay_dirs (the MPS3 pack) ----------------------------------


def test_the_openocd_config_dir_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager_mps3 import openocd
    from harness_manager_mps3.pack import Mps3Debug

    monkeypatch.delenv(openocd.CFG_DIR_ENV, raising=False)
    write_settings(f'[mps3]\nopenocd_cfg_dir = "{tmp_path / "cfg"}"\n')
    assert openocd.config_dir() == tmp_path / "cfg"
    # the scaffold adapter's search path follows the same setting (no import-time copy)
    assert Mps3Debug(None, 6921).openocd_search_paths() == (tmp_path / "cfg",)


def test_negative_twin_the_openocd_dir_variable_wins_and_nothing_is_read_at_import(tmp_path, monkeypatch):
    from harness_manager_mps3 import constants, openocd

    write_settings(f'[mps3]\nopenocd_cfg_dir = "{tmp_path / "file"}"\n')
    monkeypatch.setenv(openocd.CFG_DIR_ENV, str(tmp_path / "env"))
    assert openocd.config_dir() == tmp_path / "env"
    before = constants.OPENOCD_CFG_DIR
    reloaded = importlib.reload(constants)                   # SETTINGS.md §12.9
    assert reloaded.OPENOCD_CFG_DIR == before != tmp_path / "env"
    monkeypatch.delenv(openocd.CFG_DIR_ENV)
    write_settings("")
    assert openocd.config_dir() == openocd.OPENOCD_CFG_DIR   # neither: the built-in


def test_the_overlay_dirs_from_the_settings_file(tmp_path, monkeypatch):
    from harness_manager_mps3 import overlays

    monkeypatch.delenv(overlays.OVERLAY_DIRS_ENV, raising=False)
    write_settings(f'[mps3]\noverlay_dirs = ["{tmp_path / "a"}", "{tmp_path / "b"}"]\n')
    assert overlays.env_overlay_dirs() == [tmp_path / "a", tmp_path / "b"]
    first = overlays.default_catalogue()
    write_settings(f'[mps3]\noverlay_dirs = ["{tmp_path / "c"}"]\n')
    assert overlays.default_catalogue() is not first         # rebuilt when the setting moves


def test_negative_twin_the_overlay_variable_still_wins(tmp_path, monkeypatch):
    from harness_manager_mps3 import overlays

    write_settings(f'[mps3]\noverlay_dirs = ["{tmp_path / "file"}"]\n')
    monkeypatch.setenv(overlays.OVERLAY_DIRS_ENV, os.pathsep.join([str(tmp_path / "e1"),
                                                                   str(tmp_path / "e2")]))
    assert overlays.env_overlay_dirs() == [tmp_path / "e1", tmp_path / "e2"]


def test_overlay_dir_flags_go_ahead_of_the_settings_dirs(tmp_path, monkeypatch):
    from harness_manager.cli.cmd_program import overlay_dirs
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    name = "HARNESS_MANAGER_MPS3_OVERLAY_DIRS"
    monkeypatch.delenv(name, raising=False)
    flag = tmp_path / "flag"
    flag.mkdir()
    write_settings(f'[mps3]\noverlay_dirs = ["{tmp_path / "file"}"]\n')
    ctx = SimpleNamespace(args=SimpleNamespace(overlay_dir=[str(flag)]), pack="mps3",
                          engine=Engine(EngineConfig(state_dir=state())))
    with overlay_dirs(ctx):
        assert os.environ[name] == os.pathsep.join([str(flag), str(tmp_path / "file")])
    assert name not in os.environ
    monkeypatch.setenv(name, str(tmp_path / "env"))          # twin: ahead of the variable's
    with overlay_dirs(ctx):
        assert os.environ[name] == os.pathsep.join([str(flag), str(tmp_path / "env")])
    assert os.environ[name] == str(tmp_path / "env")


# --- mps3.console.pace_ms and mps3.mcc.*pace_ms (reopen rows: read at a board open) -----------------


def test_the_dut_console_pace_from_the_settings_applies_at_the_next_open(vboard):
    from harness_manager_mps3.pack import Mps3Pack

    pack = Mps3Pack(console_ports=vboard.console_ports)
    write_settings("[mps3.console]\npace_ms = 35\n")
    session = pack.open(pack.candidate_for_host(vboard.shell_endpoint))
    try:
        assert session.consoles.console_write_pace_s() == {"uart0": 0.035, "uart1": 0.035}
    finally:
        session.close()


def test_negative_twin_without_the_setting_the_packs_own_pace_stands(vboard):
    from harness_manager_mps3.pack import Mps3Pack

    pack = Mps3Pack(console_ports=vboard.console_ports, console_pace_s=0.0)  # --pack-overrides
    session = pack.open(pack.candidate_for_host(vboard.shell_endpoint))
    try:
        assert session.consoles.console_write_pace_s() == {"uart0": 0.0, "uart1": 0.0}
    finally:
        session.close()


def test_the_mcc_paces_from_the_settings():
    from harness_manager_mps3 import mcc

    write_settings("[mps3.mcc]\npace_ms = 80\nshare_pace_ms = 150\n")
    assert mcc.timing_for("serial:///dev/ttyUSB0").pace_s == 0.08
    assert mcc.timing_for("tcp://127.0.0.1:4000").pace_s == 0.15


def test_negative_twin_the_mcc_paces_stand_and_a_too_fast_one_is_refused():
    from harness_manager_mps3 import mcc

    assert mcc.timing_for("serial:///dev/ttyUSB0") is mcc.DEFAULT_TIMING
    assert mcc.timing_for("tcp://127.0.0.1:4000").pace_s == mcc.SHARE_PACE_S
    write_settings("[mps3.mcc]\npace_ms = 10\n")          # the MCC drops faster input
    assert mcc.timing_for("serial:///dev/ttyUSB0") is mcc.DEFAULT_TIMING


# --- the admin's lock and a bad value ------------------------------------------------------------


def test_the_admins_lock_beats_the_variable(tmp_path, monkeypatch):
    from harness_manager.services import debug

    locked = exe(tmp_path / "lab" / "openocd")
    monkeypatch.setenv(debug.OPENOCD_ENV, str(exe(tmp_path / "env" / "openocd")))
    write_policy(tmp_path, f'[lock]\ntools.openocd = "{locked}"\n')
    assert debug.find_openocd() == str(locked)


def test_negative_twin_the_admins_default_sits_below_the_variable_and_the_user(tmp_path, monkeypatch):
    from harness_manager.services import debug

    lab = exe(tmp_path / "lab" / "openocd")
    write_policy(tmp_path, f'[default]\ntools.openocd = "{lab}"\n')
    monkeypatch.delenv(debug.OPENOCD_ENV, raising=False)
    assert debug.find_openocd() == str(lab)                  # the machine's default
    mine = exe(tmp_path / "mine" / "openocd")
    write_settings(f'[tools]\nopenocd = "{mine}"\n')
    assert debug.find_openocd() == str(mine)                 # the user above it
    env_one = exe(tmp_path / "env" / "openocd")
    monkeypatch.setenv(debug.OPENOCD_ENV, str(env_one))
    assert debug.find_openocd() == str(env_one)              # the variable above both


def test_a_bad_variable_fails_naming_it(monkeypatch):
    from harness_manager.services.debug import DebugService

    monkeypatch.setenv("HARNESS_MANAGER_DEBUG_PORT_BASE", "80")
    with pytest.raises(UsageError) as exc:
        DebugService(None)._pinned_base()
    assert "$HARNESS_MANAGER_DEBUG_PORT_BASE" in exc.value.message
    assert "1024..65535" in exc.value.message


def test_negative_twin_a_bad_file_value_is_skipped_and_logged_once(monkeypatch, caplog):
    from harness_manager.services.debug import DebugService

    monkeypatch.delenv("HARNESS_MANAGER_DEBUG_PORT_BASE", raising=False)
    write_settings("[debug]\nport_base = 80\n")
    with caplog.at_level(logging.WARNING, logger="harness_manager.settings.runtime"):
        assert DebugService(None)._pinned_base() is None
        assert DebugService(None)._pinned_base() is None
    said = [r for r in caplog.records if "debug.port_base" in r.getMessage()]
    assert len(said) == 1 and "1024..65535; ignored" in said[0].getMessage()


def test_board_and_hub_rows_are_not_read_here():
    with pytest.raises(UsageError, match="per board"):
        runtime.value("boards.x.name")
    assert runtime.value("general.theme") == "auto"          # twin: an app row is


# --- the cache and settings.changed --------------------------------------------------------------


def test_settings_changed_drops_the_cache(monkeypatch):
    runtime.value("tools.openocd")
    assert runtime._now
    bus = EventBus()
    runtime.watch(bus)
    bus.publish(Event("deploy.done", "", {}))
    assert runtime._now                                      # twin: another topic
    bus.publish(Event("settings.changed", "", {"keys": ["tools.openocd"]}))
    assert not runtime._now


# --- one state-dir rule, and a service's own dir ------------------------------------------------


def _every_copy() -> dict[str, Path]:
    from harness_manager.core.session import default_lock_dir
    from harness_manager.daemon.state import default_state_dir
    from harness_manager.engine import resolve_state_dir
    from harness_manager.power.config import default_path
    from harness_manager.services.claim import ClaimService
    from harness_manager.services.debug import DebugService
    from harness_manager.services.xvc import XvcService

    return {"engine": resolve_state_dir(), "daemon": default_state_dir(),
            "locks": default_lock_dir().parent, "debug": DebugService(None).state_dir,
            "xvc": XvcService(None).state_dir, "claim": ClaimService(None).state_dir,
            "boards.toml": default_path().parent, "settings": config_dir()}


def test_every_copy_of_the_state_dir_rule_follows_the_service(tmp_path):
    use_config_dir(tmp_path / "svc")
    assert set(_every_copy().values()) == {tmp_path / "svc"}


def test_negative_twin_without_a_service_every_copy_follows_the_variable(tmp_path):
    assert set(_every_copy().values()) == {state()}
    use_config_dir(tmp_path / "svc")
    use_config_dir(None)
    assert set(_every_copy().values()) == {state()}


def _cand() -> Candidate:
    return Candidate("mps3", "mps3@10.9.8.7:6900",
                     (Link(LinkKind.ETHERNET, "10.9.8.7:6900", "shell"),))


def test_a_service_reads_its_own_boards_toml_and_hubs(tmp_path):
    from harness_manager.naming import config_name
    from harness_manager.settings import hubs

    state().mkdir(parents=True, exist_ok=True)
    (state() / "boards.toml").write_text('[boards.b]\nmatch = ["10.9.8.7"]\nname = "USER"\n')
    write_settings('[hubs.users-hub]\nhost = "real-hub"\n')
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "boards.toml").write_text('[boards.b]\nmatch = ["10.9.8.7"]\nname = "SERVICE"\n')
    write_settings('[hubs.demo]\nhost = "demo-hub"\n', root=svc)
    use_config_dir(svc)
    assert config_name(_cand()) == "SERVICE"
    assert hubs.load_resolver().instances()["hubs"] == ["demo"]


def test_negative_twin_without_the_service_dir_the_users_files_are_read(tmp_path):
    from harness_manager.naming import config_name
    from harness_manager.settings import hubs

    state().mkdir(parents=True, exist_ok=True)
    (state() / "boards.toml").write_text('[boards.b]\nmatch = ["10.9.8.7"]\nname = "USER"\n')
    write_settings('[hubs.users-hub]\nhost = "real-hub"\n')
    assert config_name(_cand()) == "USER"
    assert hubs.load_resolver().instances()["hubs"] == ["users-hub"]


# --- [boards.defaults]: the resolver's board layer, in the one boards.toml reader -------------------


BOARDS = """\
[boards.defaults]
name = "never"
match = ["10.0.0.99"]
xvc = { reach = "hub", user = "lab" }
power = { timeout_s = 5.0 }
hub = { baud = 57600 }

[boards.plain]
match = ["10.0.0.1"]

[boards.metered]
match = ["10.0.0.2"]
xvc = { user = "me" }
power = { kind = "tasmota", url = "http://10.0.0.50" }
"""


def test_boards_defaults_fill_in_every_board(tmp_path):
    from harness_manager.power.config import load_boards

    (tmp_path / "boards.toml").write_text(BOARDS)
    cfg = load_boards(tmp_path / "boards.toml")
    assert [b.key for b in cfg.boards] == ["plain", "metered"]       # defaults is no board
    plain, metered = cfg.boards
    assert dict(plain.tables["xvc"]) == {"reach": "hub", "user": "lab"}
    assert dict(metered.tables["xvc"]) == {"reach": "hub", "user": "me"}   # its own wins
    assert metered.power is not None and metered.power.timeout_s == 5.0


def test_negative_twin_defaults_never_name_match_or_switch_a_feature_on(tmp_path):
    from harness_manager.power.config import load_boards

    (tmp_path / "boards.toml").write_text(BOARDS)
    plain, metered = load_boards(tmp_path / "boards.toml").boards
    assert plain.name == "" and plain.match == ("10.0.0.1",)
    assert plain.power is None and not plain.power_error          # no meter from defaults
    assert "hub" not in plain.tables                               # no hub route either
    assert load_boards(tmp_path / "boards.toml").for_board("mps3@10.0.0.99:6900") is None


def test_the_resolver_agrees_about_a_boards_own_keys(tmp_path):
    from harness_manager.settings import Resolver

    r = Resolver(env={}, user={"boards.defaults.xvc.user": "lab", "boards.b.match": ["x"]},
                 packs=[__import__("harness_manager_mps3.pack", fromlist=["Mps3Pack"]).Mps3Pack()])
    assert r.resolve("boards.b.xvc.user").value == "lab"
    with pytest.raises(UsageError, match="its own"):
        r.check_settable("boards.defaults.name", "x")
    r2 = Resolver(env={}, user={"boards.defaults.name": "never"})
    assert r2.resolve("boards.b.name").value == ""                 # never from the defaults


# --- daemon start: the flags reach the service, and the settings fill the rest ----------------


def test_daemon_start_passes_log_level_and_pack_overrides_on():
    from harness_manager.daemon.control import service_argv

    argv = service_argv(Path("/s"), None, None, log_level="debug",
                        pack_overrides={"mps3": {"console_pace_s": 0.0}})
    assert argv == ["--state-dir", "/s", "--log-level", "debug", "--pack-overrides",
                    '{"mps3": {"console_pace_s": 0.0}}']
    assert service_argv(Path("/s"), 0, "127.0.0.1", True) == [          # twin: nothing extra
        "--state-dir", "/s", "--port", "0", "--listen", "127.0.0.1", "--demo"]


def test_the_cli_hands_both_flags_to_the_service(monkeypatch, capsys):
    from harness_manager.daemon import control
    from harness_manager.daemon.state import DaemonInfo
    from tests.fakes.t13_daemon import run_cli

    seen: dict = {}

    def fake_start(sdir, **kw):
        seen.update(kw, sdir=sdir)
        return DaemonInfo(pid=1, port=2, token="t", started_at=0.0, version="0")

    monkeypatch.setattr(control, "start", fake_start)
    rc, _, err = run_cli(capsys, "daemon", "start", "--log-level", "debug",
                         "--pack-overrides", '{"mps3": {"rbb_port": 7000}}')
    assert rc == 0, err
    assert seen["log_level"] == "debug" and seen["pack_overrides"] == {"mps3": {"rbb_port": 7000}}
    assert seen["port"] is None and seen["listen"] is None    # the service's settings decide
    rc, _, err = run_cli(capsys, "daemon", "start", "--pack-overrides", "[1]")   # twin
    assert rc == 2 and "JSON object" in err


def test_the_service_start_flags_default_to_the_settings(tmp_path):
    from harness_manager.daemon.server import start_setting

    write_settings('[advanced]\nlog_level = "debug"\nport = 23999\nlisten = "127.0.0.2"\n',
                   root=tmp_path)
    assert start_setting("log_level", None, tmp_path) == "debug"
    assert start_setting("port", None, tmp_path) == 23999
    assert start_setting("listen", None, tmp_path) == "127.0.0.2"
    assert start_setting("log_level", "warning", tmp_path) == "warning"      # a flag wins
    assert start_setting("port", None, tmp_path / "empty") == 0               # twin: defaults
    assert start_setting("listen", None, tmp_path / "empty") == "127.0.0.1"


def test_a_flag_against_the_admins_lock_is_refused(tmp_path):
    from harness_manager.daemon.server import start_setting

    write_policy(tmp_path, '[lock]\nadvanced.listen = "127.0.0.1"\n')
    with pytest.raises(RefusedError, match="--listen 0.0.0.0"):
        start_setting("listen", "0.0.0.0", tmp_path / "svc")
    assert start_setting("listen", "127.0.0.1", tmp_path / "svc") == "127.0.0.1"   # twin
    assert start_setting("listen", None, tmp_path / "svc") == "127.0.0.1"


# --- the update checker follows settings.changed ------------------------------------------------


def test_the_update_checker_checks_again_when_its_settings_change(tmp_path, monkeypatch):
    from harness_manager.daemon import update_checker as uc

    monkeypatch.setattr(uc, "WAKE_MIN_S", 0.0)
    bus = EventBus()
    checker = uc.UpdateChecker(lambda: None, bus, tmp_path, first_delay_s=3600)
    ticks: list[float] = []
    done = threading.Event()

    def tick():
        ticks.append(time.monotonic())
        done.set()
        return {"skipped": "test", "interval_s": 3600}

    checker.tick = tick
    checker.start()
    try:
        bus.publish(Event("settings.changed", "", {"keys": ["general.theme"]}))
        assert not done.wait(0.5)                            # twin: not a key it reads
        bus.publish(Event("settings.changed", "", {"keys": ["updates.channel"]}))
        assert done.wait(5.0) and len(ticks) == 1
    finally:
        checker.stop()
