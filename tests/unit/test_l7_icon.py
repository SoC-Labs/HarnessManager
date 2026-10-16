"""Lane L7: app icon and identity: build_icons.py, window.py's taskbar identity, the macOS
bundle script, and the install scripts' icon steps (read statically; no Windows here)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import types
from pathlib import Path

import pytest

from harness_manager.web import window

ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = ROOT / "packaging" / "icon"
spec = importlib.util.spec_from_file_location("build_icons", ICON_DIR / "build_icons.py")
build_icons = importlib.util.module_from_spec(spec)
sys.modules["build_icons"] = build_icons
spec.loader.exec_module(build_icons)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _have_renderer() -> bool:
    try:
        build_icons.renderer_name()
        return True
    except build_icons.NoRenderer:
        return False


needs_renderer = pytest.mark.skipif(not _have_renderer(), reason="no SVG renderer installed")


def _png(size: int) -> bytes:
    return PNG_MAGIC + struct.pack(">I", size) + b"x" * 8


def test_three_candidates_exist_and_are_svg():
    names = sorted(p.name for p in (ICON_DIR / "candidates").glob("*.svg"))
    assert len(names) == 3 and names[0].startswith("A") and names[2].startswith("C")
    for n in names:
        text = (ICON_DIR / "candidates" / n).read_text()
        assert "<text" not in text                        # no text in the glyph


def test_ico_container_lists_every_size():
    sizes = build_icons.ICO_SIZES
    ico = build_icons.make_ico({s: _png(s) for s in sizes})
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind, count) == (0, 1, len(sizes))
    for i, s in enumerate(sorted(sizes)):
        w, h, _c, _r, _planes, bpp, length, offset = struct.unpack("<BBBBHHII", ico[6 + 16 * i:22 + 16 * i])
        assert (w, h) == ((0, 0) if s >= 256 else (s, s)) and bpp == 32
        assert ico[offset:offset + length] == _png(s)


def test_icns_container_has_every_element():
    pngs = {s: _png(s) for s in build_icons.ICNS_TYPES}
    icns = build_icons.make_icns(pngs)
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns)
    seen, pos = [], 8
    while pos < len(icns):
        kind, length = icns[pos:pos + 4], struct.unpack(">I", icns[pos + 4:pos + 8])[0]
        seen.append(kind)
        pos += length
    assert seen == [build_icons.ICNS_TYPES[s] for s in sorted(build_icons.ICNS_TYPES)]


@needs_renderer
def test_build_writes_every_output_with_the_right_sizes(tmp_path: Path):
    out = tmp_path / "out"
    files = build_icons.build(ICON_DIR / "candidates" / "A-board.svg", out)
    assert all(f.is_file() for f in files)
    for s in build_icons.PNG_SIZES:
        png = (out / "hicolor" / f"{s}x{s}" / "apps" / "harness-manager.png").read_bytes()
        assert png[:8] == PNG_MAGIC
        assert struct.unpack(">II", png[16:24]) == (s, s)
    assert (out / "harness-manager.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert (out / "harness-manager.icns").read_bytes()[:4] == b"icns"


@needs_renderer
def test_build_is_reproducible(tmp_path: Path):
    master = ICON_DIR / "candidates" / "C-chip-led.svg"
    build_icons.build(master, tmp_path / "one")
    build_icons.build(master, tmp_path / "two")
    for rel in ("harness-manager.ico", "harness-manager.icns", "hicolor/16x16/apps/harness-manager.png"):
        assert (tmp_path / "one" / rel).read_bytes() == (tmp_path / "two" / rel).read_bytes()


def test_build_rejects_a_missing_master(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        build_icons.build(tmp_path / "nope.svg", tmp_path / "out")


def test_the_committed_build_matches_the_default_master_sizes():
    build = ICON_DIR / "build"
    for s in build_icons.PNG_SIZES:
        assert (build / "hicolor" / f"{s}x{s}" / "apps" / "harness-manager.png").is_file()
    assert (build / "harness-manager.ico").is_file() and (build / "harness-manager.icns").is_file()


# -- window.py ------------------------------------------------------------------------------

class FakeShell32:
    def __init__(self, result: int = 0) -> None:
        self.calls: list[str] = []
        self.result = result

    def SetCurrentProcessExplicitAppUserModelID(self, ident: str) -> int:  # noqa: N802
        self.calls.append(ident)
        return self.result


def test_app_user_model_id_is_set_on_windows_only():
    shell = FakeShell32()
    assert window.set_app_user_model_id("win32", shell) is True
    assert shell.calls == [window.APP_USER_MODEL_ID]
    for other in ("linux", "darwin"):
        shell = FakeShell32()
        assert window.set_app_user_model_id(other, shell) is False
        assert shell.calls == []


def test_app_user_model_id_failure_is_tolerated():
    assert window.set_app_user_model_id("win32", FakeShell32(result=-1)) is False

    class Boom:
        def SetCurrentProcessExplicitAppUserModelID(self, _ident):  # noqa: N802
            raise OSError("no shell32")

    assert window.set_app_user_model_id("win32", Boom()) is False


def test_the_id_matches_the_installers_and_the_desktop_class():
    assert window.APP_USER_MODEL_ID in (ROOT / "scripts" / "install.ps1").read_text()
    assert window.APP_USER_MODEL_ID in (ROOT / "packaging" / "macos" / "Info.plist.in").read_text()
    desktop = (ROOT / "packaging" / "linux" / "harness-manager.desktop").read_text()
    assert f"StartupWMClass={window.WM_CLASS}" in desktop


def test_find_icon_prefers_the_installed_file_and_is_per_platform(tmp_path: Path, monkeypatch):
    home = tmp_path / "root"
    (home / "icon").mkdir(parents=True)
    (home / "icon" / "harness-manager.ico").write_bytes(b"i")
    monkeypatch.setenv("HARNESS_MANAGER_HOME", str(home))
    assert window.find_icon("win32") == home / "icon" / "harness-manager.ico"
    # no installed PNG: the checkout's built one
    assert window.find_icon("linux").name == "harness-manager.png"
    (home / "icon" / "harness-manager.png").write_bytes(b"p")
    assert window.find_icon("linux") == home / "icon" / "harness-manager.png"


def test_start_gets_the_icon_only_when_pywebview_takes_it(tmp_path: Path):
    png = tmp_path / "i.png"
    png.write_bytes(b"x")
    new, old = types.ModuleType("w"), types.ModuleType("w")
    new.start = lambda func=None, icon=None: None
    old.start = lambda func=None: None
    assert window._start_kwargs(new, png) == {"icon": str(png)}
    assert window._start_kwargs(old, png) == {}
    assert window._start_kwargs(new, None) == {}
    assert window._start_kwargs(new, tmp_path / "i.ico") == {}


def test_pywebview_path_sets_the_identity_then_starts(monkeypatch):
    seen: list[object] = []
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: seen.append("create")
    fake.start = lambda func=None, icon=None: seen.append(("start", icon))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(window, "pywebview_backend_missing", lambda: None)
    monkeypatch.setattr(window, "_prepare_identity", lambda icon: seen.append("identity"))
    got = window._try_pywebview("http://x")
    assert got is not None and got.how == "pywebview"
    assert seen[0] == "identity" and seen[1] == "create" and seen[2][0] == "start"


# -- the macOS bundle and the install scripts -----------------------------------------------

MAKE_APP = ROOT / "packaging" / "macos" / "make_app.sh"


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="bash needed")
def test_make_app_builds_an_idempotent_removable_bundle(tmp_path: Path):
    app = tmp_path / "Apps" / "Harness Manager.app"
    app.parent.mkdir()
    icns = tmp_path / "i.icns"
    icns.write_bytes(b"icns")
    cmd = [str(MAKE_APP), str(app), "/opt/hm/bin/harness-manager", str(icns), "9.9.9"]
    for _ in range(2):                                    # twice: a re-run replaces it
        subprocess.run(cmd, check=True, capture_output=True)
    plist = (app / "Contents" / "Info.plist").read_text()
    assert "uk.ac.soton.soclabs.harness-manager" in plist and "9.9.9" in plist
    assert "<key>LSUIElement</key>\n    <false/>" in plist
    assert "<string>Harness Manager</string>" in plist and "harness-manager</string>" in plist
    exe = app / "Contents" / "MacOS" / "harness-manager-app"
    assert os.access(exe, os.X_OK) and 'exec "/opt/hm/bin/harness-manager" app' in exe.read_text()
    assert (app / "Contents" / "Resources" / "harness-manager.icns").read_bytes() == b"icns"
    if shutil.which("plutil"):
        subprocess.run(["plutil", "-lint", str(app / "Contents" / "Info.plist")], check=True)
    subprocess.run([str(MAKE_APP), "--remove", str(app)], check=True, capture_output=True)
    assert not app.exists()


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="bash needed")
def test_make_app_leaves_a_foreign_bundle_alone(tmp_path: Path):
    app = tmp_path / "Harness Manager.app"
    app.mkdir()
    icns = tmp_path / "i.icns"
    icns.write_bytes(b"icns")
    res = subprocess.run([str(MAKE_APP), str(app), "/x/hm", str(icns)], capture_output=True, text=True)
    assert res.returncode == 2 and "left" in res.stderr
    subprocess.run([str(MAKE_APP), "--remove", str(app)], check=True, capture_output=True)
    assert app.exists()


def test_install_sh_installs_icons_and_the_mac_bundle_and_removes_them():
    sh = (ROOT / "scripts" / "install.sh").read_text()
    assert "icon_png_dir" in sh and "hicolor/${n}x${n}/apps/harness-manager.png" in sh
    assert "mac_app_entry" in sh and "remove_mac_app" in sh and "Harness Manager.app" in sh
    uninstall = sh.split("if [[ $uninstall -eq 1 ]]", 1)[1].split("exit 0", 1)[0]
    assert "remove_desktop_entry" in uninstall and "remove_mac_app" in uninstall
    if sys.platform != "win32":      # the Windows runner's bash is the WSL stub: no POSIX shell
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / "install.sh")], check=True)


def test_install_ps1_stamps_the_shortcut_with_icon_and_id():
    ps1 = (ROOT / "scripts" / "install.ps1").read_text()
    assert "$lnk.IconLocation" in ps1 and "harness-manager.ico" in ps1
    assert "Set-ShortcutAppId $StartMenuLink $AppUserModelId" in ps1
    assert "PKEY_AppUserModel_ID" in ps1 and "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3" in ps1
