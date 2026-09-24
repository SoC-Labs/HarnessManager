"""OTA-L: the self-updater's side of the install layout (M2, M4-M7), the dev guard, and the
administrator's policy file (U6).

``FakeUv`` builds nothing; the install root is a directory in ``tmp_path``. Each
behaviour has a negative twin.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from harness_manager import _launch as L
from harness_manager.core.errors import RefusedError
from harness_manager.services.update.app import (
    AppLayout,
    AppUpdater,
    LocalBusyProbe,
    lock_names,
    wheel_extras,
)
from harness_manager.services.update.policy import (
    DEFAULT_CHECK_INTERVAL_S,
    MIN_CHECK_INTERVAL_S,
    Policy,
    load_policy,
    parse_interval,
    parse_policy,
    policy_path,
)
from harness_manager.services.update.schema import AppRelease, Asset
from harness_manager.services.update.service import UpdateService
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_board import FakeUv

KEYS = TestKeys()
SHA = "cd" * 32
METADATA = """Metadata-Version: 2.1
Name: harness-manager
Version: {version}
Requires-Dist: fastapi>=0.110
Provides-Extra: serial
Requires-Dist: pyserial>=3.5; extra == "serial"
Provides-Extra: app
Requires-Dist: pywebview>=5; extra == 'app'
Requires-Dist: proxy_tools; sys_platform == "darwin" and extra == "app"
"""
LOCK = """# uv pip compile --universal --generate-hashes
fastapi==0.110.0 \\
    --hash=sha256:{h}
PySerial==3.5 \\
    --hash=sha256:{h}
mps3-pyverify @ file:///x/mps3_pyverify-0.1.0-py3-none-any.whl --hash=sha256:{h}
""".format(h="ab" * 32)


def wheel(path: Path, version: str = "0.2.0") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"harness_manager-{version}.dist-info/METADATA",
                    METADATA.format(version=version))
    return path


def release(version: str) -> AppRelease:
    w = Asset(name=f"harness_manager-{version}-py3-none-any.whl", url="https://x/w.whl",
              sha256=SHA, size=10)
    return AppRelease(version=version, status="current", wheel=w)


def install_root(tmp_path: Path, *, extras=(), uv: str = "") -> dict:
    root = tmp_path / "root"
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("installer python")
    L.register(root, venv, "0.1.0", extras=list(extras), uv=uv, windows=False)
    return {"root": root, "venv": venv}


def updater(root: Path, state_dir: Path, **kw) -> tuple[AppUpdater, FakeUv]:
    uv = FakeUv()
    up = AppUpdater(AppLayout(root), LocalBusyProbe(state_dir), uv="/opt/uv", runner=uv,
                    python_version="3.11", running_version="0.1.0", windows=False, **kw)
    return up, uv


# --- M2: rollback reaches the installer's venv ------------------------------------------------

def test_rollback_right_after_the_first_update_returns_to_the_installed_version(tmp_path):
    inst = install_root(tmp_path)
    up, _ = updater(inst["root"], tmp_path / "state")
    up.stage(release("0.2.0"), wheel(tmp_path / "w.whl"))
    up.switch("0.2.0")
    st = up.rollback()                      # the spike's step 7: exit 15 before this lane
    assert (st["current"], st["previous"]) == ("", "0.2.0")
    assert st["installer"]["version"] == "0.1.0"     # the registration survives every save
    st = up.rollback()                      # and forward again
    assert (st["current"], st["previous"]) == ("0.2.0", "")


@pytest.mark.parametrize("case", ["never switched", "not registered", "venv gone"])
def test_negative_twin_rollback_to_the_installer_is_refused_when_it_cannot_work(tmp_path, case):
    inst = install_root(tmp_path)
    up, _ = updater(inst["root"], tmp_path / "state")
    if case != "never switched":
        up.stage(release("0.2.0"), wheel(tmp_path / "w.whl"))
        up.switch("0.2.0")
    if case == "not registered":
        st = json.loads(up.layout.pointer.read_text())
        st.pop("installer")
        up.layout.pointer.write_text(json.dumps(st))
    if case == "venv gone":
        (inst["venv"] / "bin" / "python").unlink()
    with pytest.raises(RefusedError, match="no previous version|is gone"):
        up.rollback()


# --- the dev guard and the policy's "off" -----------------------------------------------------

def test_a_developer_install_never_stages_switches_or_rolls_back(tmp_path):
    inst = install_root(tmp_path)
    up, uv = updater(inst["root"], tmp_path / "state", dev_install="this is a developer install")
    for call in (lambda: up.stage(release("0.2.0"), wheel(tmp_path / "w.whl")),
                 lambda: up.switch("0.2.0"), up.rollback):
        with pytest.raises(RefusedError, match="developer install"):
            call()
    assert uv.calls == [] and json.loads(up.layout.pointer.read_text())["current"] == ""


def test_negative_twin_policy_off_still_allows_rollback(tmp_path):
    inst = install_root(tmp_path)
    up, _ = updater(inst["root"], tmp_path / "state")
    up.stage(release("0.2.0"), wheel(tmp_path / "w.whl"))
    up.switch("0.2.0")
    up.policy_off = "the administrator's policy /etc/x turns self-update off"
    with pytest.raises(RefusedError, match="administrator"):
        up.stage(release("0.3.0"), wheel(tmp_path / "w3.whl", "0.3.0"))
    assert up.rollback()["current"] == ""


# --- M4, M5, M6, M7: the updater of the running copy ---------------------------------------------

def _site(venv: Path) -> str:
    return str(venv / "lib" / "python3.11" / "site-packages" / "harness_manager" / "_launch.py")


@pytest.fixture
def as_installed(monkeypatch):
    """Make ``_launch.dev_install`` judge the fake prefix as an installed copy."""
    real = L.dev_install

    def fake(prefix=None, env=None, **_kw):
        return real(prefix, env or {}, module_file=_site(Path(prefix)), editable=False)

    monkeypatch.setattr(L, "dev_install", fake)
    monkeypatch.delenv("HARNESS_MANAGER_UV", raising=False)


def test_for_install_uses_the_install_root_uv_and_extras(tmp_path, as_installed, monkeypatch):
    uv = tmp_path / "root" / "venv" / "bin" / "uv"
    inst = install_root(tmp_path, extras=("serial",), uv=str(uv))
    uv.write_text("uv")
    # M7: a state-dir override elsewhere does not move the pointer
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "elsewhere"))
    for prefix in (inst["venv"], inst["root"] / "versions" / "0.2.0"):
        up = AppUpdater.for_install(tmp_path / "elsewhere", prefix=str(prefix))
        assert up.layout.root == inst["root"] and up.layout.pointer == inst["root"] / "current.json"
        assert up.dev_install == "" and up.uv == str(uv) and up.extras == ("serial",)
        assert up.find_uv() == str(uv)
    # the environment's uv still wins (the spike, tests)
    monkeypatch.setenv("HARNESS_MANAGER_UV", "/opt/other/uv")
    assert AppUpdater.for_install(tmp_path, prefix=str(inst["venv"])).find_uv() == "/opt/other/uv"


def test_negative_twin_for_install_of_a_developer_venv_is_guarded(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_UV", raising=False)
    up = AppUpdater.for_install(tmp_path / "state")          # this checkout's own venv
    assert up.dev_install and up.layout.root == tmp_path / "state" / "update" / "app"
    assert up.blocked() == up.dev_install


def test_negative_twin_for_install_of_a_venv_no_installer_made(tmp_path, as_installed):
    inst = install_root(tmp_path)
    (inst["root"] / "install.json").write_text(json.dumps({"venv": "/somewhere/else"}))
    up = AppUpdater.for_install(tmp_path / "state", prefix=str(inst["venv"]))
    assert "not made by the Harness Manager installer" in up.dev_install
    assert up.layout.root == tmp_path / "state" / "update" / "app"


def test_extras_the_lock_covers_are_kept(tmp_path):
    w = wheel(tmp_path / "w.whl")
    assert wheel_extras(w) == {"serial": {"pyserial"}, "app": {"pywebview", "proxy-tools"}}
    assert lock_names(LOCK) == {"fastapi", "pyserial", "mps3-pyverify"}
    lock = tmp_path / "lock.txt"
    lock.write_text(LOCK)
    inst = install_root(tmp_path)
    up, uv = updater(inst["root"], tmp_path / "state", extras=("serial", "app"))
    assert up.kept_extras(w, lock) == (["serial"], ["app"])
    rec = up.stage(release("0.2.0"), w, lock)
    assert rec["extras"] == ["serial"] and rec["extras_missing"] == ["app"]
    assert "\nharness-manager[serial] @ file://" in uv.reqs_seen[0]


def test_negative_twin_no_extras_no_lock_no_brackets(tmp_path):
    w = wheel(tmp_path / "w.whl")
    inst = install_root(tmp_path)
    up, uv = updater(inst["root"], tmp_path / "state", extras=("serial",))
    assert up.kept_extras(w, None) == ([], ["serial"])       # no lock: nothing is covered
    up2, uv2 = updater(inst["root"], tmp_path / "state2")
    assert up2.kept_extras(w, None) == ([], [])
    up2.stage(release("0.2.0"), w)
    assert "\nharness-manager @ file://" in "\n" + uv2.reqs_seen[0]


# --- the policy file (U6) -----------------------------------------------------------------------

def test_policy_paths_per_os():
    assert str(policy_path("linux", {})) == "/etc/harness-manager/policy.toml"
    assert str(policy_path("darwin", {})).startswith("/Library/Application Support/harness-manager")
    win = policy_path("win32", {"ProgramData": r"D:\PD"})
    assert win.parts[-2:] == ("harness-manager", "policy.toml") and str(win).startswith("D:")


def test_policy_parses_the_three_settings(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('self_update = "notify"\nchannel = "stable"\ncheck_interval = "12h"\n')
    pol = load_policy(p)
    assert (pol.self_update, pol.channel, pol.interval_s) == ("notify", "stable", 12 * 3600)
    assert pol.off_reason() == "" and pol.problems == ()
    assert pol.channel_for(None) == "stable" and pol.channel_for("stable") == "stable"
    with pytest.raises(RefusedError, match="pins the 'stable' channel"):
        pol.channel_for("beta")
    assert parse_policy("self_update = false").self_update == "off"
    assert parse_policy("self_update = true").self_update == "stage"
    assert parse_policy('check_interval = 0').interval_s == 0
    assert parse_interval("90m") == 5400 and parse_interval(3600) == 3600


def test_negative_twin_no_policy_file_is_no_policy(tmp_path):
    pol = load_policy(tmp_path / "absent.toml")
    assert pol == Policy() and pol.off_reason() == "" and pol.channel_for("beta") == "beta"
    assert pol.interval_s == DEFAULT_CHECK_INTERVAL_S


@pytest.mark.parametrize("text, why", [
    ("self_update = ", "not valid TOML"),
    ('self_update = "sometimes"', "not one of"),
    ('channel = "Beta Channel"', "not a channel name"),
    ('self_update = "off"', 'self_update = "off"'),
])
def test_policy_fails_closed(tmp_path, text, why):
    p = tmp_path / "policy.toml"
    p.write_text(text + "\n")
    pol = load_policy(p)
    assert pol.self_update == "off" and why in pol.off_reason() and str(p) in pol.off_reason()


def test_negative_twin_policy_warnings_do_not_turn_it_off(tmp_path):
    pol = parse_policy('check_interval = "soon"\ncolour = "blue"\ncheck_interval_x = 1\n')
    assert pol.self_update == "stage" and pol.off_reason() == ""
    assert pol.interval_s == DEFAULT_CHECK_INTERVAL_S and len(pol.problems) == 3
    assert parse_policy("check_interval = 5").interval_s == MIN_CHECK_INTERVAL_S


def test_an_unreadable_policy_fails_closed(tmp_path):
    p = tmp_path / "policy.toml"
    p.mkdir()                                  # reading a directory: an OSError
    assert "cannot be read" in load_policy(p).off_reason()


# --- the service with a policy and the dev guard ---------------------------------------------

@pytest.fixture
def channel_app(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        b = ChannelBuilder(srv.root, KEYS)
        b.add_app("0.2.0", AssetFile("harness_manager-0.2.0-py3-none-any.whl", b"PK-fake-wheel"),
                  lock=AssetFile("harness_manager-0.2.0-requirements.lock",
                                 b"fastapi==0.110 --hash=sha256:" + b"ab" * 32 + b"\n"))
        b.publish(serial=1)
        yield srv


def service(tmp_path, *, policy: Policy, **kw) -> tuple[UpdateService, FakeUv]:
    inst = install_root(tmp_path)
    up, uv = updater(inst["root"], tmp_path / "state", policy_off=policy.off_reason(), **kw)
    return UpdateService(state_dir=tmp_path / "state", trust=KEYS.trust(), app_version="0.1.0",
                         app_updater=up, token="", policy=policy), uv


def test_policy_off_refuses_the_update_before_any_download(channel_app, tmp_path):
    pol = parse_policy('self_update = "off"', "/etc/harness-manager/policy.toml")
    svc, uv = service(tmp_path, policy=pol)
    report = svc.check(source=channel_app.source())
    assert report["app_update"] == "" and report["policy"]["self_update"] == "off"
    assert any("0.2.0 is available, but self-update is off" in w for w in report["warnings"])
    with pytest.raises(RefusedError, match="administrator's policy"):
        svc.update_app(source=channel_app.source())
    assert uv.calls == [] and not list((tmp_path / "state").glob("update/cache/blobs/*"))


def test_negative_twin_without_a_policy_the_update_is_offered_and_staged(channel_app, tmp_path):
    svc, uv = service(tmp_path, policy=Policy())
    report = svc.check(source=channel_app.source())
    assert report["app_update"] == "0.2.0" and "policy" not in report
    assert svc.update_app(source=channel_app.source())["switched"]


def test_the_pinned_channel_is_used_and_another_is_refused(channel_app, tmp_path):
    svc, _ = service(tmp_path, policy=parse_policy('channel = "stable"', "/etc/p.toml"))
    assert svc.check(source=channel_app.source())["channel"] == "stable"
    with pytest.raises(RefusedError, match="pins the 'stable' channel"):
        svc.check(channel="beta", source=channel_app.source())


def test_a_developer_install_is_told_why_there_is_no_update(channel_app, tmp_path):
    svc, uv = service(tmp_path, policy=Policy(), dev_install="this is a developer install")
    report = svc.check(source=channel_app.source())
    assert report["app_update"] == "" and any("developer install" in w for w in report["warnings"])
    with pytest.raises(RefusedError, match="developer install"):
        svc.update_app(source=channel_app.source())
    assert uv.calls == []
