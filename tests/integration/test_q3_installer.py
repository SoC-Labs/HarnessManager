"""Q3 install: ``scripts/install.sh`` robustness, offline and in a throwaway HOME.

Every install here runs from a hand-made wheelhouse (``--offline``): a stand-in
harness-manager wheel whose command answers ``version`` and ``daemon stop``, a stand-in
pyverify wheel, and two versions of a stand-in dependency. So these tests need no
network and take a few seconds each, and they exercise the real installer end to end:
the venv, pip, the lock, the pins, the launcher, the desktop menu entry, the messages.

``test_l5_install.py`` (and CI's distribution matrix) run the real package from PyPI.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(os.name == "nt", reason="install.sh is for Linux and macOS"),
]

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install.sh"
LINUX = sys.platform.startswith("linux")

FAKE_CLI = '''\
import os
import sys

VERSION = "{version}"


def main():
    args = sys.argv[1:]
    if args == ["version"]:
        print(VERSION)
        return 0
    if args == ["--version"]:
        print("Harness Manager " + VERSION)
        return 0
    if args[:2] == ["daemon", "stop"]:
        # 8 = ALREADY: no service was running; the tests set 6 to play a stop that failed
        return int(os.environ.get("HM_Q3_STOP_EXIT", "8"))
    print("stand-in harness-manager", *args)
    return 0
'''


# -- a wheelhouse by hand ------------------------------------------------------------------

def _record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def make_wheel(dest: Path, name: str, version: str, files: dict[str, str], *,
               requires: tuple[str, ...] = (), extras: dict[str, tuple[str, ...]] | None = None,
               scripts: dict[str, str] | None = None) -> Path:
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    for extra, reqs in (extras or {}).items():
        meta.append(f"Provides-Extra: {extra}")
        meta += [f'Requires-Dist: {r}; extra == "{extra}"' for r in reqs]
    content = {path: text.encode() for path, text in files.items()}
    content[f"{info}/METADATA"] = ("\n".join(meta) + "\n").encode()
    content[f"{info}/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: test_q3_installer\n"
                                b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    if scripts:
        lines = ["[console_scripts]"] + [f"{k} = {v}" for k, v in scripts.items()]
        content[f"{info}/entry_points.txt"] = ("\n".join(lines) + "\n").encode()
    record = [_record_line(p, d) for p, d in content.items()] + [f"{info}/RECORD,,"]
    content[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
    whl = dest / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for path, data in content.items():
            zf.writestr(path, data)
    return whl


def harness_wheel(dest: Path, version: str, *, requires=("fakedep>=1",)) -> Path:
    return make_wheel(dest, "harness-manager", version,
                      {"hm_q3_standin.py": FAKE_CLI.format(version=version)},
                      requires=requires, extras={"serial": ("fakeserial",)},
                      scripts={"harness-manager": "hm_q3_standin:main"})


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    wh = tmp_path / "wheelhouse"
    wh.mkdir()
    harness_wheel(wh, "0.1.0")
    make_wheel(wh, "mps3-pyverify", "0.1.0", {"pyverify_q3_standin.py": "X = 1\n"})
    make_wheel(wh, "fakedep", "1.0", {"fakedep.py": "V = '1.0'\n"})
    make_wheel(wh, "fakedep", "2.0", {"fakedep.py": "V = '2.0'\n"})
    make_wheel(wh, "fakeserial", "1.0", {"fakeserial.py": "V = '1.0'\n"})
    (wh / "constraints.txt").write_text("fakedep==1.0\n")
    return wh


# -- running the installer -------------------------------------------------------------------

class Box:
    """A throwaway HOME, and the installer run inside it with a controlled environment."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.bin = self.home / ".local" / "bin"
        self.hm = self.bin / "harness-manager"
        self.root = self.home / ".local" / "share" / "harness-manager"
        self.venv = self.root / "venv"
        self.state = self.home / ".config" / "harness-manager"
        self.menu = self.home / ".local" / "share" / "applications" / "harness-manager.desktop"
        self.icon = (self.home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
                     / "harness-manager.svg")
        self.base_path = os.pathsep.join(p for p in ("/usr/local/bin", "/usr/bin", "/bin")
                                         if Path(p).is_dir())

    def env(self, *, on_path: bool = False, path: str | None = None, **extra: str) -> dict:
        env = {
            "HOME": str(self.home),
            "USER": os.environ.get("USER", "q3"),
            "LANG": "C.UTF-8",
            "SHELL": "/bin/bash",
            "PATH": path or ((f"{self.bin}{os.pathsep}" if on_path else "") + self.base_path),
            "HARNESS_MANAGER_STATE_DIR": str(self.state),
            "HARNESS_MANAGER_PTY_DIR": str(self.home / "pty"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "UV_CACHE_DIR": str(self.home / "uv-cache"),
            "TMPDIR": str(self.home),
        }
        env.update(extra)
        return env

    def run(self, *args: str, env: dict | None = None, timeout: float = 240,
            check: bool | None = True) -> subprocess.CompletedProcess:
        res = subprocess.run(["bash", str(INSTALL), *args], env=env or self.env(),
                             cwd=self.home, capture_output=True, text=True, timeout=timeout)
        if check is not None and (res.returncode == 0) != check:
            raise AssertionError(f"install.sh {' '.join(args)} exited {res.returncode}\n"
                                 f"--- stdout\n{res.stdout}\n--- stderr\n{res.stderr}")
        return res

    def hm_run(self, *args: str) -> str:
        return subprocess.run([str(self.hm), *args], env=self.env(), capture_output=True,
                              text=True, check=True, timeout=60).stdout.strip()

    def py(self, code: str) -> str:
        return subprocess.run([str(self.venv / "bin" / "python"), "-c", code], env=self.env(),
                              capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def offline(wheelhouse: Path, *more: str) -> list[str]:
    return ["--offline", str(wheelhouse), "--python", sys.executable, "--no-uv", *more]


# -- the tests ---------------------------------------------------------------------------------

def test_help_lists_every_option():
    out = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True,
                         check=True).stdout
    for opt in ("--offline DIR", "--latest", "--no-desktop", "--desktop", "--with-serial",
                "--uninstall", "HTTPS_PROXY"):
        assert opt in out, opt


def test_lifecycle_install_rerun_upgrade_uninstall(box: Box, wheelhouse: Path, tmp_path: Path):
    # A stale lock from a killed install (its pid is gone) must not block the next one.
    (box.root / ".install.lock").mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (box.root / ".install.lock" / "pid").write_text(f"{dead.pid}\n")

    # 1. First install, with an extra; ~/.local/bin is not on PATH.
    res = box.run(*offline(wheelhouse, "--with-serial"))
    assert box.hm_run("version") == "0.1.0"
    assert box.hm_run("--version") == "Harness Manager 0.1.0"
    assert box.py("import fakeserial, fakedep; print(fakedep.V)") == "1.0"   # the pin
    assert "is not on your PATH yet" in res.stdout
    assert 'export PATH="$HOME/.local/bin:$PATH"' in res.stdout
    assert f"  {box.hm} app --demo" in res.stdout       # Next: runs by its full path
    assert not (box.root / ".install.lock").exists()
    assert "extras=serial" in (box.root / "install.conf").read_text()
    if LINUX:
        menu = box.menu.read_text()
        assert f'Exec="{box.hm}" app' in menu and f'Exec="{box.hm}" app --demo' in menu
        assert f"Icon={box.icon}" in menu and box.icon.is_file()
        assert "StartupWMClass=harness-manager" in menu
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(box.menu)], check=True)
    else:
        assert not box.menu.exists()

    # 2. Re-run with no options: idempotent, keeps the extra, keeps the settings.
    box.state.mkdir(parents=True, exist_ok=True)
    (box.state / "boards.toml").write_text('[boards."mps3@192.168.10.101:6900"]\n')
    res = box.run(*offline(wheelhouse), env=box.env(on_path=True))
    assert "upgrade  " in res.stdout
    assert "[serial]" in res.stdout
    assert "not on your PATH" not in res.stdout
    assert "  harness-manager app --demo" in res.stdout
    assert (box.state / "boards.toml").read_text().startswith("[boards.")

    # 3. Upgrade to a newer wheel.
    newer = tmp_path / "newer"
    newer.mkdir()
    harness_wheel(newer, "0.2.0")
    box.run(*offline(wheelhouse, "--from", str(newer / "harness_manager-0.2.0-py3-none-any.whl")))
    assert box.hm_run("version") == "0.2.0"
    assert box.py("import fakeserial; print('kept')") == "kept"

    # 4. --no-desktop removes the menu entry and is remembered; --desktop brings it back.
    if LINUX:
        box.run(*offline(wheelhouse, "--no-desktop"))
        assert not box.menu.exists() and not box.icon.exists()
        box.run(*offline(wheelhouse))
        assert not box.menu.exists()
        box.run(*offline(wheelhouse, "--desktop"))
        assert box.menu.exists()

    # 5. Uninstall: the command, the venv, the menu entry and the record go; settings stay.
    res = box.run("--uninstall")
    assert not box.hm.exists() and not box.venv.exists()
    assert not box.menu.exists() and not box.icon.exists()
    assert not box.root.exists()
    assert (box.state / "boards.toml").exists()
    assert "kept" in res.stdout


def test_pins_hold_unless_latest(box: Box, wheelhouse: Path):
    res = box.run(*offline(wheelhouse))
    assert "pins     the tested dependency versions" in res.stdout
    assert box.py("import fakedep; print(fakedep.V)") == "1.0"
    res = box.run(*offline(wheelhouse, "--latest"))
    assert "pins     " not in res.stdout and "latest   rebuilding" in res.stdout
    assert box.py("import fakedep; print(fakedep.V)") == "2.0"
    # Back on the pins, an upgrade in place moves the dependency back to the tested one.
    box.run(*offline(wheelhouse))
    assert box.py("import fakedep; print(fakedep.V)") == "1.0"


@pytest.mark.skipif(not shutil.which("uv"), reason="uv is not on PATH")
def test_uv_offline_install(box: Box, wheelhouse: Path):
    uv_dir = str(Path(shutil.which("uv")).parent)
    env = box.env(path=f"{uv_dir}{os.pathsep}{box.base_path}")
    box.run("--offline", str(wheelhouse), "--python", sys.executable, env=env)
    assert box.hm_run("version") == "0.1.0"
    assert box.py("import fakedep; print(fakedep.V)") == "1.0"


def test_second_install_at_once_is_refused(box: Box, wheelhouse: Path):
    lock = box.root / ".install.lock"
    lock.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (lock / "pid").write_text(f"{holder.pid}\n")
        t0 = time.monotonic()
        res = box.run(*offline(wheelhouse), check=False)
        assert res.returncode == 1
        assert f"another Harness Manager install (pid {holder.pid}) is running" in res.stderr
        assert str(lock) in res.stderr                     # how to clear a lock by hand
        assert time.monotonic() - t0 < 20
        assert lock.is_dir() and not box.venv.exists()     # the holder's lock is untouched
        box.run("--uninstall", check=False)                # also waits its turn
        assert lock.is_dir()
    finally:
        holder.kill()
        holder.wait()


def test_interrupted_install_resumes(box: Box, wheelhouse: Path):
    proc = subprocess.Popen(["bash", str(INSTALL), *offline(wheelhouse)], env=box.env(),
                            cwd=box.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    deadline = time.monotonic() + 60
    while not box.venv.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is not None:
        pytest.skip("the install finished before it could be interrupted")
    os.killpg(proc.pid, signal.SIGINT)                      # Ctrl-C: the whole process group
    _out, err = proc.communicate(timeout=120)
    assert proc.returncode != 0
    if proc.returncode == 130:
        assert "interrupted; run it again" in err
    assert not (box.root / ".install.lock").exists()
    assert not box.hm.exists()
    box.run(*offline(wheelhouse))
    assert box.hm_run("version") == "0.1.0"


def _tool_dir(where: Path, *, python3_version: str | None) -> str:
    """A PATH with the basic tools and no Python >= 3.10 (and no uv)."""
    where.mkdir()
    tools = ("bash sh env uname id mkdir dirname basename cat sed grep mktemp tar rm sleep tr "
             "head readlink mv chmod cp ln rmdir").split()
    for tool in tools:
        found = shutil.which(tool, path="/usr/bin:/bin")
        if found:
            (where / tool).symlink_to(found)
    if python3_version:
        fake = where / "python3"
        fake.write_text("#!/bin/sh\ncase \"$2\" in *python_version*) echo "
                        f"{python3_version} ;; *) exit 1 ;; esac\n")
        fake.chmod(0o755)
    return str(where)


def test_no_new_enough_python_says_what_to_install(box: Box, wheelhouse: Path, tmp_path: Path):
    path = _tool_dir(tmp_path / "tools", python3_version="3.6.8")
    osr = tmp_path / "os-release"
    osr.write_text('NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n')
    env = box.env(path=path, HARNESS_MANAGER_OS_RELEASE=str(osr))
    res = box.run("--offline", str(wheelhouse), env=env, check=False)
    assert res.returncode == 1
    assert "needs Python 3.10 or newer. Found only python3 (3.6.8)." in res.stderr
    assert "sudo dnf install python3.12" in res.stderr
    assert "astral.sh/uv/install.sh" in res.stderr
    assert "--python /path/to/python3.12" in res.stderr
    assert not box.root.exists()                  # nothing left behind

    osr.write_text('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="20.04"\n')
    res = box.run("--offline", str(wheelhouse), env=env, check=False)
    assert "sudo apt install python3 python3-venv" in res.stderr


def test_no_write_access_is_reported_before_any_work(box: Box, wheelhouse: Path, tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        res = box.run(*offline(wheelhouse),
                      env=box.env(HARNESS_MANAGER_BIN_DIR=str(ro / "bin")), check=False)
        assert res.returncode == 1
        assert f"cannot write to {ro / 'bin'} (where the command goes)" in res.stderr
        assert "HARNESS_MANAGER_BIN_DIR" in res.stderr
        assert not box.venv.exists()
        res = box.run(*offline(wheelhouse),
                      env=box.env(HARNESS_MANAGER_HOME=str(ro / "hm")), check=False)
        assert "(the install root)" in res.stderr and "HARNESS_MANAGER_HOME" in res.stderr
    finally:
        ro.chmod(0o755)


def test_no_index_explains_proxy_and_offline(box: Box, tmp_path: Path):
    # A wheel whose dependency is nowhere local, and an index that does not answer.
    alone = tmp_path / "alone"
    alone.mkdir()
    whl = harness_wheel(alone, "0.1.0", requires=("fakedep-nowhere>=1",))
    make_wheel(alone, "mps3-pyverify", "0.1.0", {"pyverify_q3_standin.py": "X = 1\n"})
    env = box.env(PIP_INDEX_URL="http://127.0.0.1:9/simple", PIP_RETRIES="0", PIP_TIMEOUT="2")
    res = box.run("--from", str(whl), "--python", sys.executable, "--no-uv", env=env,
                  check=False)
    assert res.returncode == 1
    assert "the install did not finish" in res.stderr
    assert "HTTPS_PROXY" in res.stderr and "--offline DIR" in res.stderr
    assert "Running this again is safe" in res.stderr
    assert not box.hm.exists()


def test_offline_needs_a_directory(box: Box, tmp_path: Path):
    res = box.run("--offline", str(tmp_path / "missing"), check=False)
    assert res.returncode == 1 and "is not a directory" in res.stderr
    assert "make_wheelhouse.sh" in res.stderr
    res = box.run("--offline", str(tmp_path), "--from", "git@github.com:SoC-Labs/x.git",
                  check=False)
    assert res.returncode == 1 and "--offline cannot clone" in res.stderr


def test_a_menu_entry_we_did_not_write_is_left_alone(box: Box, wheelhouse: Path):
    if not LINUX:
        pytest.skip("the menu entry is Linux-only")
    box.menu.parent.mkdir(parents=True)
    box.menu.write_text("[Desktop Entry]\nName=Someone else's\n")
    res = box.run(*offline(wheelhouse))
    assert "left " + str(box.menu) + " alone" in res.stderr
    box.run("--uninstall")
    assert box.menu.read_text().startswith("[Desktop Entry]\nName=Someone")


def _zombie() -> subprocess.Popen:
    """A child that has exited and that nobody reaps: what a container's PID 1 leaves."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 10
    while Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return child


def _daemon_json(state: Path, pid: int) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "daemon.json").write_text(
        f'{{\n  "hostname": "x",\n  "pid": {pid},\n  "port": 1,\n  "token": "t"\n}}\n')


@pytest.mark.skipif(not LINUX, reason="zombies are visible through /proc on Linux")
def test_upgrade_carries_on_when_the_service_already_exited(box: Box, wheelhouse: Path):
    # CI run 35993990472: in a container, the stopped demo service stayed a zombie, and an
    # installed harness-manager from before the fix said "did not stop" (exit 6).
    box.run(*offline(wheelhouse))
    zombie = _zombie()
    try:
        _daemon_json(box.state / "demo", zombie.pid)
        res = box.run(*offline(wheelhouse), env=box.env(HM_Q3_STOP_EXIT="6"))
        assert "the Harness Manager service (demo) had already exited" in res.stderr
        assert "the Harness Manager service had already exited" in res.stderr  # no daemon.json
    finally:
        zombie.wait()


def test_negative_twin_upgrade_stops_when_the_service_really_runs(box: Box, wheelhouse: Path):
    box.run(*offline(wheelhouse))
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _daemon_json(box.state / "demo", live.pid)
        res = box.run(*offline(wheelhouse), env=box.env(HM_Q3_STOP_EXIT="6"), check=False)
        assert res.returncode == 1
        assert "did not stop (`harness-manager daemon stop --demo` exited 6)" in res.stderr
    finally:
        live.kill()
        live.wait()


@pytest.mark.parametrize("distro, want", [
    ('ID="rocky"\nID_LIKE="rhel centos fedora"\n', "sudo dnf install python3.12-pip"),
    ('ID=fedora\n', "sudo dnf install python3-pip"),
    ('ID=ubuntu\nID_LIKE=debian\n', "sudo apt install python3.12-venv"),
])
def test_a_python_that_cannot_make_a_venv_names_the_package(box: Box, wheelhouse: Path,
                                                             tmp_path: Path, distro, want):
    # CI run 35993990472: Rocky 8's python3.12 without python3.12-pip, told to apt install.
    path = _tool_dir(tmp_path / "tools", python3_version=None)
    fake = Path(path) / "python3.12"
    fake.write_text('#!/bin/sh\ncase "$*" in\n'
                    '  *print*sys.version_info*) echo 3.12 ;;\n'
                    '  *sys.version_info*) exit 0 ;;\n'
                    '  *python_version*) echo 3.12.1 ;;\n'
                    '  *"-m venv"*) echo "Error: ensurepip returned non-zero exit status 1." >&2;'
                    ' exit 1 ;;\nesac\n')
    fake.chmod(0o755)
    osr = tmp_path / "os-release"
    osr.write_text(distro)
    res = box.run("--offline", str(wheelhouse), "--no-uv",
                  env=box.env(path=path, HARNESS_MANAGER_OS_RELEASE=str(osr)), check=False)
    assert res.returncode == 1
    assert "python3.12 could not make a venv with pip in it" in res.stderr
    assert want in res.stderr, res.stderr
    assert "ensurepip returned non-zero" in res.stderr       # the tool's own error, too
    assert not box.venv.exists()
