"""Q3 install: ``scripts/install.sh`` robustness, offline and in a throwaway HOME.

Every install here runs from a hand-made wheelhouse (``--offline``): a stand-in
harness-manager wheel whose command answers ``version`` and ``daemon stop``, a stand-in
pyverify wheel, and two versions of a stand-in dependency. So these tests need no
network and take a few seconds each, and they exercise the real installer end to end:
the venv, pip, the lock, the pins, the launcher, the desktop menu entry, the messages.

The stand-in is a ``harness_manager`` package with the REAL ``harness_manager/_launch.py``
(lane OTA-L), so the launcher, the self-update pointer, the install record and the
migration of an older layout run for real, against real venvs.

``test_l5_install.py`` (and CI's distribution matrix) run the real package from PyPI.
"""

from __future__ import annotations

import base64
import hashlib
import json
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


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["version"]:
        print(VERSION)
        return 0
    if args[:1] == ["exit"]:
        return int(args[1])
    if args == ["whoami"]:
        print(VERSION, sys.prefix)
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


LAUNCH_PY = (ROOT / "src" / "harness_manager" / "_launch.py").read_text()


def standin_package(version: str, *, launcher: bool = True) -> dict[str, str]:
    """A stand-in harness_manager: its CLI, and the real launcher (a wheel before OTA-L: none)."""
    files = {"harness_manager/__init__.py": f'__version__ = "{version}"\n',
             "harness_manager/cli/__init__.py": "",
             "harness_manager/cli/main.py": FAKE_CLI.format(version=version)}
    if launcher:
        files["harness_manager/_launch.py"] = LAUNCH_PY
    return files


def harness_wheel(dest: Path, version: str, *, requires=("fakedep>=1",),
                  launcher: bool = True) -> Path:
    scripts = {"harness-manager": "harness_manager.cli.main:main"}
    if launcher:
        scripts["harness-manager-launch"] = "harness_manager._launch:main"
    return make_wheel(dest, "harness-manager", version, standin_package(version, launcher=launcher),
                      requires=requires, extras={"serial": ("fakeserial",)}, scripts=scripts)


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
        self.pointer = self.root / "current.json"
        self.install_json = self.root / "install.json"
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

    def hm_run(self, *args: str, **env: str) -> str:
        return self.hm_proc(*args, check=True, **env).stdout.strip()

    def hm_proc(self, *args: str, check: bool = False, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.hm), *args], env=self.env(**env), capture_output=True,
                              text=True, check=check, timeout=60)

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
                "--no-path", "--path",
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
    # G5: the installer edits the files itself; FIX-PACK-3's rule (a login file, not
    # ~/.bashrc alone) now applies to the files it writes.
    assert "/.bashrc" in res.stdout and "/.bash_profile" in res.stdout
    assert "Until then, run it by its full path" in res.stdout
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
             "head readlink mv chmod cp ln rmdir cut").split()
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
    # CI run 35993990472: Rocky 8's python3.12 could not make a venv and was told to apt install.
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


def test_rhel8_old_expat_is_named_as_the_cause(box: Box, wheelhouse: Path, tmp_path: Path):
    # CI run 36006058809, reproduced in a Rocky 8 rootfs: the image's expat 2.2.5 is older
    # than python3.12's pyexpat needs, so ensurepip fails inside `python3.12 -m venv`, which
    # hides why. The installer makes a venv without pip, runs ensurepip itself, and says so.
    path = _tool_dir(tmp_path / "tools", python3_version=None)
    err = ("ImportError: /usr/lib64/python3.12/lib-dynload/pyexpat.cpython-312-x86_64-linux-gnu"
           ".so: undefined symbol: XML_SetBillionLaughsAttackProtectionMaximumAmplification")
    probe_python = (f'#!/bin/sh\necho "Traceback (most recent call last):" >&2\n'
                    f'echo "{err}" >&2\n'
                    'echo "subprocess.CalledProcessError: Command returned 1." >&2\nexit 1\n')
    fake = Path(path) / "python3.12"
    fake.write_text('#!/bin/sh\ncase "$*" in\n'
                    '  *print*sys.version_info*) echo 3.12 ;;\n'
                    '  *sys.version_info*) exit 0 ;;\n'
                    '  *python_version*) echo 3.12.14 ;;\n'
                    '  *"--without-pip"*) d="$4"; mkdir -p "$d/bin";'
                    f" printf '%s' '{probe_python}' > \"$d/bin/python\";"
                    ' chmod 755 "$d/bin/python" ;;\n'
                    '  *"-m venv"*) echo "Error: Command ensurepip returned non-zero exit status 1." >&2;'
                    ' exit 1 ;;\nesac\n')
    fake.chmod(0o755)
    osr = tmp_path / "os-release"
    osr.write_text('ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="8.10"\n')
    res = box.run("--offline", str(wheelhouse), "--no-uv",
                  env=box.env(path=path, HARNESS_MANAGER_OS_RELEASE=str(osr)), check=False)
    assert res.returncode == 1
    assert f"install.sh: the cause: {err}" in res.stderr, res.stderr
    assert "Update expat: sudo dnf upgrade expat, then run this again." in res.stderr
    assert "CalledProcessError" not in res.stderr.split("the cause:")[1]


# -- OTA-L: the launcher, the install record and the self-update layout ------------------------

def make_version(venv: Path, version: str) -> Path:
    """A real venv holding a stand-in harness_manager ``version``: a self-updated version."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   timeout=180)
    site = subprocess.run([str(venv / "bin" / "python"), "-c",
                           "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    for rel, text in standin_package(version).items():
        f = Path(site) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return venv


def point(box: Box, current: str, **more) -> dict:
    ptr = json.loads(box.pointer.read_text())
    ptr.update(current=current, **more)
    box.pointer.write_text(json.dumps(ptr))
    return ptr


def test_the_launcher_follows_the_pointer_and_passes_the_exit_code(box: Box, wheelhouse: Path):
    box.run(*offline(wheelhouse))
    ptr = json.loads(box.pointer.read_text())
    assert ptr["current"] == "" and ptr["installer"] == {"version": "0.1.0", "venv": str(box.venv)}
    v2 = make_version(box.root / "versions" / "0.2.0", "0.2.0")
    point(box, "0.2.0")
    assert box.hm_run("version") == "0.2.0"
    assert box.hm_run("whoami") == f"0.2.0 {v2}"         # the new venv's own Python runs it
    assert box.hm_proc("exit", "7").returncode == 7
    assert box.hm_proc("exit", "0").returncode == 0
    # the way back when a self-updated version cannot start
    assert box.hm_run("version", HARNESS_MANAGER_USE_INSTALLED="1") == "0.1.0"


def test_negative_twin_the_launcher_falls_back_to_the_installed_version(box: Box, wheelhouse: Path):
    box.run(*offline(wheelhouse))
    assert box.hm_run("whoami") == f"0.1.0 {box.venv}"
    point(box, "0.2.0")                                  # its venv is not there
    res = box.hm_proc("version")
    assert res.stdout.strip() == "0.1.0" and "0.2.0 is missing" in res.stderr
    assert box.hm_proc("exit", "3").returncode == 3      # in this process, the code passes too
    make_version(box.root / "versions" / "0.2.0", "0.2.0")
    assert box.hm_run("version") == "0.2.0"
    # the dev guard: never follow the pointer
    assert box.hm_run("version", HARNESS_MANAGER_NO_SELF_UPDATE="1") == "0.1.0"


@pytest.mark.parametrize("selfupdated, runs", [("0.0.9", "0.1.0"), ("0.2.0", "0.2.0")])
def test_a_rerun_of_the_installer_and_a_self_updated_version(box: Box, wheelhouse: Path,
                                                             selfupdated: str, runs: str):
    box.run(*offline(wheelhouse))
    make_version(box.root / "versions" / selfupdated, selfupdated)
    point(box, selfupdated, previous="")
    assert box.hm_run("version") == selfupdated
    res = box.run(*offline(wheelhouse))
    assert box.hm_run("version") == runs
    ptr = json.loads(box.pointer.read_text())
    if runs == "0.1.0":
        # M3: the installer installs a version at least as new: it wins, and the
        # self-updated one stays as the rollback target
        assert (ptr["current"], ptr["previous"]) == ("", "0.0.9")
        assert "the command runs 0.1.0 now" in res.stdout
    else:
        # negative twin: a newer self-updated version keeps running; one rollback reaches 0.1.0
        assert (ptr["current"], ptr["previous"]) == ("0.2.0", "")
        assert "is newer than 0.1.0" in res.stdout


def test_the_install_record_extras_and_uv(box: Box, wheelhouse: Path):
    # M5 twin first: a wheelhouse without uv installs, and says the app cannot update itself
    res = box.run(*offline(wheelhouse, "--with-serial"))
    assert "no uv in the venv (the wheelhouse has no uv wheel)" in res.stderr
    info = json.loads(box.install_json.read_text())
    assert (info["venv"], info["version"], info["extras"], info["uv"], info["installer"]) == \
        (str(box.venv), "0.1.0", ["serial"], "", "install.sh")
    # M5: with uv in the wheelhouse, it goes into the venv, and the record names it
    make_wheel(wheelhouse, "uv", "0.8.0", {"uv_q3_standin.py": "def main():\n    print('uv')\n"},
               scripts={"uv": "uv_q3_standin:main"})
    res = box.run(*offline(wheelhouse))
    assert "no uv" not in res.stderr
    info = json.loads(box.install_json.read_text())
    assert info["uv"] == str(box.venv / "bin" / "uv") and (box.venv / "bin" / "uv").exists()
    assert info["extras"] == ["serial"]                 # M6: kept by a re-run without the flag


def test_an_older_install_s_self_updated_versions_move_into_the_install_root(box: Box,
                                                                            wheelhouse: Path):
    # M4: an older updater built its venvs in the state dir
    legacy = box.state / "update" / "app"
    make_version(legacy / "versions" / "0.2.0", "0.2.0")
    (legacy / "current.json").write_text(json.dumps(
        {"current": "0.2.0", "previous": "", "versions": {"0.2.0": {"state": "staged"}}}))
    res = box.run(*offline(wheelhouse))
    moved = box.root / "versions" / "0.2.0"
    assert f"moved    self-updated version 0.2.0 to {moved}" in res.stdout
    assert not legacy.exists() and moved.is_dir()
    # The moved venv runs (its python, not its scripts' #! lines); it is newer, so it stays.
    assert box.hm_run("whoami") == f"0.2.0 {moved}"
    assert json.loads(box.pointer.read_text())["versions"] == {"0.2.0": {"state": "staged"}}
    # --uninstall removes the self-updated versions too; the state dir stays
    res = box.run("--uninstall")
    assert f"removed  {box.root / 'versions'}" in res.stdout
    assert not box.root.exists() and box.state.is_dir()


def test_the_state_dir_does_not_move_the_pointer(box: Box, wheelhouse: Path, tmp_path: Path):
    # M7: installed with one state dir, run with others: the pointer is the install root's
    box.run(*offline(wheelhouse), env=box.env(HARNESS_MANAGER_STATE_DIR=str(tmp_path / "s1")))
    make_version(box.root / "versions" / "0.2.0", "0.2.0")
    point(box, "0.2.0")
    assert box.hm_run("version") == "0.2.0"
    assert box.hm_run("version", HARNESS_MANAGER_STATE_DIR=str(tmp_path / "s2")) == "0.2.0"
    # negative twin: the old place is never read at run time
    point(box, "")
    legacy = box.state / "update" / "app"
    make_version(legacy / "versions" / "0.3.0", "0.3.0")
    (legacy / "current.json").write_text(json.dumps({"current": "0.3.0"}))
    assert box.hm_run("version") == "0.1.0"


def test_a_wheel_from_before_the_launcher_runs_directly(box: Box, wheelhouse: Path, tmp_path: Path):
    box.run(*offline(wheelhouse))
    assert box.install_json.exists()
    old = tmp_path / "old"
    old.mkdir()
    whl = harness_wheel(old, "0.0.5", launcher=False)
    res = box.run(*offline(wheelhouse, "--from", str(whl)))
    assert "harness-manager 0.0.5 has no self-update launcher" in res.stderr
    assert box.hm_run("version") == "0.0.5" and not box.install_json.exists()


PWSH = shutil.which("pwsh")


@pytest.mark.skipif(not PWSH, reason="pwsh is not on PATH (CI's ubuntu runners have it)")
def test_install_ps1_launcher_record_extras_and_uninstall(box: Box, wheelhouse: Path):
    # install.ps1 under PowerShell 7 on this OS: the same code paths as on Windows, with
    # bin/ for Scripts\. PIP_NO_INDEX keeps pip off the network, as --offline does.
    env = box.env(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheelhouse),
                  HARNESS_MANAGER_HOME=str(box.root), HARNESS_MANAGER_BIN_DIR=str(box.bin),
                  DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1", POWERSHELL_TELEMETRY_OPTOUT="1")
    ps1 = ROOT / "scripts" / "install.ps1"
    whl = wheelhouse / "harness_manager-0.1.0-py3-none-any.whl"

    def ps(*args: str) -> subprocess.CompletedProcess:
        res = subprocess.run([PWSH, "-NoProfile", "-File", str(ps1), *args], env=env,
                             cwd=box.home, capture_output=True, text=True, timeout=300)
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        return res

    res = ps("-From", str(whl), "-Python", sys.executable, "-NoUv", "-WithSerial")
    assert "no uv in the venv" in res.stdout + res.stderr
    info = json.loads(box.install_json.read_text())
    assert (info["extras"], info["installer"], info["uv"]) == (["serial"], "install.ps1", "")
    assert box.hm.read_bytes() == (box.venv / "bin" / "harness-manager-launch").read_bytes()
    assert box.hm_run("version") == "0.1.0"
    make_version(box.root / "versions" / "0.2.0", "0.2.0")
    point(box, "0.2.0")
    assert box.hm_run("version") == "0.2.0"
    assert box.hm_proc("exit", "9").returncode == 9
    # A re-run without -WithSerial keeps the extra (install.json). 0.2.0 is newer than the
    # 0.1.0 it installs, so 0.2.0 keeps running, and one rollback reaches 0.1.0.
    res = ps("-From", str(whl), "-Python", sys.executable, "-NoUv")
    assert "[serial]" in res.stdout
    assert json.loads(box.install_json.read_text())["extras"] == ["serial"]
    assert (json.loads(box.pointer.read_text())["current"], box.hm_run("version")) == ("0.2.0",
                                                                                       "0.2.0")
    assert json.loads(box.pointer.read_text())["previous"] == ""
    res = ps("-Uninstall", "-Force")
    assert f"removed  {box.root / 'versions'}" in res.stdout
    assert not box.hm.exists() and not box.root.exists()


# -- FIX-PACK-3 item 1: the PATH advice, per shell ------------------------------------------------
#
# P8 (a clean RHEL account) followed "add export PATH=... to ~/.bashrc" and still got
# `harness-manager: command not found` over ssh: a login shell reads ~/.bash_profile (or
# ~/.bash_login, or ~/.profile: the first that exists) and never ~/.bashrc unless that file
# sources it. tcsh/csh read neither ~/.bashrc nor ~/.profile. install.sh's path_advice runs
# here on its own, with each $SHELL; the behaviour tests run the advised line in a real
# login shell.

OLD_BASH_ADVICE = ("Add this line to ~/.bashrc, then open a new terminal:\n"
                   '    export PATH="$HOME/.local/bin:$PATH"\n')          # before FIX-PACK-3
OLD_FALLBACK_ADVICE = ("Add this line to ~/.profile, then log in again:\n"
                       '    export PATH="$HOME/.local/bin:$PATH"\n')
LOGIN_FILES = ("~/.bash_profile", "~/.bash_login", "~/.profile")


def names_a_login_file(text: str) -> bool:
    return any(f in text for f in LOGIN_FILES)


def path_advice(shell: str, home: Path, os_name: str = "Linux") -> str:
    """install.sh's ``path_advice`` alone, as the installer calls it."""
    import re

    fn = re.search(r"^path_advice\(\) \{.*?^\}\n", INSTALL.read_text(), re.M | re.S)
    assert fn, "install.sh has no path_advice function"
    script = ("set -euo pipefail\nsay() { printf '%s\\n' \"$*\"; }\n" + fn.group(0)
              + 'path_advice "$1" "\\$HOME/.local/bin" "$2/.local/bin" "$3" "$2"\n')
    return subprocess.run(["bash", "-c", script, "_", shell, str(home), os_name],
                          capture_output=True, text=True, check=True).stdout


def test_bash_advice_names_the_login_file_as_well_as_bashrc(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    out = path_advice("bash", home)
    assert "~/.bashrc" in out and "~/.bash_profile" in out     # no profile yet: bash's first
    assert "not ~/.bashrc" in out                                # says why
    assert 'export PATH="$HOME/.local/bin:$PATH"' in out
    (home / ".profile").write_text("# Debian's\n")               # the one a login shell reads
    out = path_advice("bash", home)
    assert "~/.profile" in out and "~/.bash_profile" not in out
    (home / ".bash_profile").write_text("# RHEL's\n")            # wins over ~/.profile
    assert "~/.bash_profile" in path_advice("bash", home)
    mac = path_advice("bash", tmp_path / "none", "Darwin")        # Terminal: login shells
    assert "~/.bash_profile" in mac and "~/.bashrc" not in mac


def test_negative_twin_the_old_bash_advice_names_no_login_file():
    assert not names_a_login_file(OLD_BASH_ADVICE)


def test_zsh_fish_tcsh_csh_and_the_fallback(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    zsh = path_advice("zsh", home)
    assert "~/.zshrc" in zsh and "~/.zprofile" in zsh and "export PATH=" in zsh
    assert f"fish_add_path {home}/.local/bin" in path_advice("fish", home)
    for sh in ("tcsh", "csh"):
        out = path_advice(sh, home)
        assert "~/.cshrc" in out and "set path = ( $HOME/.local/bin $path )" in out, sh
        assert "export" not in out and "~/.profile" not in out, sh
    (home / ".tcshrc").write_text("")                             # tcsh reads it before .cshrc
    assert "~/.tcshrc" in path_advice("tcsh", home)
    assert "~/.cshrc" in path_advice("csh", home)                 # csh never reads ~/.tcshrc
    for sh in ("sh", "dash", "ksh", "something-else"):
        out = path_advice(sh, home)
        assert "~/.profile" in out and 'export PATH="$HOME/.local/bin:$PATH"' in out, sh


def _advised(out: str) -> tuple[str, str]:
    """(the file, the line) of a one-file advice."""
    import re

    rc = re.search(r"~/(\.[a-z_]+)", out).group(1)
    line = next(ln.strip() for ln in out.splitlines() if ln.startswith("    "))
    return rc, line


def _login_path(shell: list[str], home: Path) -> list[str]:
    """PATH as a fresh login shell in ``home`` sees it (what ssh and `bash -lc` get)."""
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "USER": os.environ.get("USER", "q3")}
    res = subprocess.run([*shell, 'echo "PATH=$PATH"'], env=env, capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL)
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("PATH=")][-1]
    return line[len("PATH="):].split(":")


@pytest.mark.skipif(not LINUX, reason="the login-shell files differ on macOS (Terminal)")
def test_following_the_bash_advice_reaches_a_login_shell(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    out = path_advice("bash", home)
    line = next(ln.strip() for ln in out.splitlines() if ln.startswith("    "))
    for rc in (".bashrc", ".bash_profile"):           # both files the advice names
        (home / rc).write_text(line + "\n")
    assert str(home / ".local" / "bin") in _login_path(["bash", "-lc"], home)


@pytest.mark.skipif(not LINUX, reason="the login-shell files differ on macOS (Terminal)")
def test_negative_twin_the_old_bash_advice_does_not_reach_a_login_shell(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    rc, line = _advised(OLD_BASH_ADVICE)
    (home / rc).write_text(line + "\n")                # ~/.bashrc only: the P8 finding
    assert str(home / ".local" / "bin") not in _login_path(["bash", "-lc"], home)


@pytest.mark.skipif(not shutil.which("tcsh"), reason="tcsh is not installed")
def test_following_the_tcsh_advice_reaches_tcsh(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    rc, line = _advised(path_advice("tcsh", home))
    (home / rc).write_text(line + "\n")
    assert _login_path(["tcsh", "-c"], home)[0] == str(home / ".local" / "bin")


@pytest.mark.skipif(not shutil.which("tcsh"), reason="tcsh is not installed")
def test_negative_twin_the_old_fallback_does_not_reach_tcsh(tmp_path: Path):
    home = tmp_path / "h"
    home.mkdir()
    rc, line = _advised(OLD_FALLBACK_ADVICE)
    (home / rc).write_text(line + "\n")                # ~/.profile: tcsh never reads it
    assert str(home / ".local" / "bin") not in _login_path(["tcsh", "-c"], home)


OLD_GUIDE_HINT = ("`harness-manager: command not found`: `~/.local/bin` is not on your PATH. "
                  "The installer printed the line to add and the full path to use until then.")


def test_the_docs_give_the_installers_advice():
    """INSTALL.md's table, README and USER_GUIDE agree with path_advice."""
    install = (ROOT / "docs" / "INSTALL.md").read_text()
    sec = install.split("## When harness-manager is not on PATH", 1)[1].split("\n## ", 1)[0]
    for want in ("~/.bashrc", "~/.bash_profile", "~/.bash_login", "~/.profile", "~/.zshrc",
                 "~/.zprofile", "fish_add_path", "set path = ( $HOME/.local/bin $path )",
                 "~/.tcshrc", "~/.cshrc", "full path"):
        assert want in sec, want
    for doc in ("README.md", "docs/USER_GUIDE.md"):
        text = (ROOT / doc).read_text()
        hint = text[text.index("command not found"):][:700]
        assert names_a_login_file(hint) and "#when-harness-manager-is-not-on-path" in hint, doc


def test_negative_twin_the_old_guide_hint_named_no_file():
    assert not names_a_login_file(OLD_GUIDE_HINT)


# -- G5: the installer puts the command on PATH itself -------------------------------------------

BEGIN = "# >>> harness-manager PATH (written by scripts/install.sh) >>>"
LINE = 'export PATH="$HOME/.local/bin:$PATH"'


def test_g5_path_block_is_added_once_and_reaches_a_login_shell(box: Box, wheelhouse: Path):
    (box.home / ".bashrc").write_text("# mine\nalias a=b")          # no trailing newline
    res = box.run(*offline(wheelhouse))
    assert "added the PATH block to" in res.stdout and "--no-path" in res.stdout
    for f in (".bashrc", ".bash_profile"):
        text = (box.home / f).read_text()
        assert text.count(BEGIN) == 1 and text.count(LINE) == 1, f
    assert (box.home / ".bashrc").read_text().startswith("# mine\nalias a=b\n")
    box.run(*offline(wheelhouse))                                   # re-install: no duplicate
    box.run(*offline(wheelhouse))
    for f in (".bashrc", ".bash_profile"):
        assert (box.home / f).read_text().count(BEGIN) == 1, f
    if LINUX:
        assert str(box.bin) in _login_path(["bash", "-lc"], box.home)
    # Uninstall takes the block out and keeps the user's own lines.
    box.run("--uninstall")
    assert BEGIN not in (box.home / ".bashrc").read_text()
    assert "alias a=b" in (box.home / ".bashrc").read_text()


def test_g5_negative_twin_no_path_and_on_path_edit_nothing(box: Box, wheelhouse: Path):
    res = box.run(*offline(wheelhouse, "--no-path"))
    assert "--no-path: your shell files are left alone" in res.stdout
    assert not (box.home / ".bashrc").exists() and not (box.home / ".bash_profile").exists()
    assert "extras=" in (box.root / "install.conf").read_text()
    assert "path=0" in (box.root / "install.conf").read_text()
    box.run(*offline(wheelhouse))                                   # remembered
    assert not (box.home / ".bashrc").exists()
    box.run(*offline(wheelhouse, "--path"))                         # asked again
    assert BEGIN in (box.home / ".bashrc").read_text()
    box.run("--uninstall")


def test_g5_on_path_install_edits_no_file(box: Box, wheelhouse: Path):
    box.run(*offline(wheelhouse), env=box.env(on_path=True))
    assert not (box.home / ".bashrc").exists() and not (box.home / ".bash_profile").exists()


def test_g5_login_file_choice_zsh_and_unquotable_shells(box: Box, wheelhouse: Path):
    (box.home / ".profile").write_text("# debian\n")      # bash reads this, not a new .bash_profile
    box.run(*offline(wheelhouse))
    assert BEGIN in (box.home / ".profile").read_text()
    assert not (box.home / ".bash_profile").exists()
    box.run("--uninstall")
    box.run(*offline(wheelhouse), env=box.env(SHELL="/bin/zsh"))
    assert BEGIN in (box.home / ".zshrc").read_text() and BEGIN in (box.home / ".zprofile").read_text()
    box.run("--uninstall")
    res = box.run(*offline(wheelhouse), env=box.env(SHELL="/usr/bin/fish"))   # advice only
    assert "fish_add_path" in res.stdout and BEGIN not in (box.home / ".zshrc").read_text()


def test_g5_a_changed_bin_dir_replaces_the_old_block(box: Box, wheelhouse: Path):
    box.run(*offline(wheelhouse))
    other = box.home / "tools" / "bin"
    box.run(*offline(wheelhouse), env=box.env(HARNESS_MANAGER_BIN_DIR=str(other)))
    text = (box.home / ".bashrc").read_text()
    assert text.count(BEGIN) == 1 and "$HOME/tools/bin" in text and ".local/bin" not in text


def test_g5_macos_dock_text_only_on_darwin(box: Box, wheelhouse: Path):
    res = box.run(*offline(wheelhouse))
    assert ("Keep in Dock" in res.stdout) == (not LINUX)
    assert "--no-desktop" in (INSTALL.read_text())
