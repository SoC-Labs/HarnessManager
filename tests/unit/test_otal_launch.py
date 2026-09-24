"""OTA-L: the install's ``harness-manager`` launcher (``harness_manager._launch``).

In-process and board-free: every test builds a fake install root in ``tmp_path`` and
passes the prefix, the environment and the dev-install verdict in, so nothing here reads
the real ``sys.prefix`` or the user's install. Each behaviour has a negative twin.
The real exec (a real venv, the real installer) is in
``tests/integration/test_q3_installer.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_manager import _launch as L


def make_install(tmp_path: Path, *, versions: tuple[str, ...] = (), current: str = "",
                 previous: str = "", windows: bool = False) -> dict:
    root = tmp_path / "root"
    venv = root / "venv"
    L.python_in(venv, windows=windows).parent.mkdir(parents=True)
    L.python_in(venv, windows=windows).write_text("installer python")
    (root / "install.json").write_text(json.dumps({"venv": str(venv), "version": "0.1.0"}))
    for v in versions:
        py = L.python_in(root / "versions" / L.version_dir(v), windows=windows)
        py.parent.mkdir(parents=True)
        py.write_text(f"python {v}")
    if current or previous:
        (root / "current.json").write_text(json.dumps({"current": current, "previous": previous}))
    return {"root": root, "venv": venv}


# --- where an install lives ---------------------------------------------------------------

def test_install_root_of_the_installer_venv_and_of_a_self_updated_version(tmp_path):
    inst = make_install(tmp_path, versions=("0.2.0",))
    assert L.install_root(inst["venv"]) == inst["root"]
    assert L.install_root(inst["root"] / "versions" / "0.2.0") == inst["root"]


def test_negative_twin_no_install_root_without_an_install_json_naming_the_venv(tmp_path):
    inst = make_install(tmp_path)
    other = tmp_path / "elsewhere" / "venv"
    other.mkdir(parents=True)
    assert L.install_root(other) is None                          # no install.json beside it
    (inst["root"] / "install.json").write_text(json.dumps({"venv": str(other)}))
    assert L.install_root(inst["venv"]) is None                   # it names another venv
    (inst["root"] / "install.json").write_text("not json")
    assert L.install_root(inst["venv"]) is None


# --- the dev guard --------------------------------------------------------------------------

def _inside(venv: Path) -> str:
    return str(venv / "lib" / "python3.11" / "site-packages" / "harness_manager" / "_launch.py")


def test_an_installed_copy_is_not_a_developer_install(tmp_path):
    inst = make_install(tmp_path)
    assert L.dev_install(inst["venv"], {}, module_file=_inside(inst["venv"]), editable=False) == ""


@pytest.mark.parametrize("case", ["env", "outside", "editable", "foreign"])
def test_negative_twin_each_developer_install_is_recognised(tmp_path, case):
    inst = make_install(tmp_path)
    venv, env, module, editable = inst["venv"], {}, _inside(inst["venv"]), False
    if case == "env":
        env = {L.NO_SELF_UPDATE_ENV: "1"}
    elif case == "outside":                        # pip install -e, or PYTHONPATH=src
        module = str(tmp_path / "checkout" / "src" / "harness_manager" / "_launch.py")
    elif case == "editable":                       # direct_url.json: dir_info.editable
        editable = True
    else:                                          # a venv no installer made
        venv = tmp_path / "dev" / ".venv"
        module = _inside(venv)
    why = L.dev_install(venv, env, module_file=module, editable=editable)
    assert why, case
    assert {"env": L.NO_SELF_UPDATE_ENV, "outside": "developer install",
            "editable": "pip install -e", "foreign": "not made by"}[case] in why


def test_this_checkout_is_a_developer_install():
    # The test suite runs from an editable install: the real check must say so.
    assert L.dev_install() != ""
    assert L.selected() == (None, "")


def test_no_self_update_zero_means_unset(tmp_path):
    inst = make_install(tmp_path)
    for value in ("", "0", "false", "no", "off"):
        env = {L.NO_SELF_UPDATE_ENV: value}
        assert L.dev_install(inst["venv"], env, module_file=_inside(inst["venv"]),
                             editable=False) == "", value


# --- which version runs ---------------------------------------------------------------------

def test_the_pointer_selects_the_self_updated_version(tmp_path):
    inst = make_install(tmp_path, versions=("0.2.0",), current="0.2.0")
    py, note = L.selected(inst["venv"], {}, windows=False, dev="")
    assert py == inst["root"] / "versions" / "0.2.0" / "bin" / "python" and note == ""
    py, _ = L.selected(inst["venv"], {}, windows=True, dev="")    # Windows: Scripts\python.exe
    assert py is None                                            # (not built for Windows here)


def test_windows_layout_is_followed_on_windows(tmp_path):
    inst = make_install(tmp_path, versions=("0.2.0",), current="0.2.0", windows=True)
    py, _ = L.selected(inst["venv"], {}, windows=True, dev="")
    assert py == inst["root"] / "versions" / "0.2.0" / "Scripts" / "python.exe"


def test_a_local_version_label_maps_to_its_directory_name(tmp_path):
    inst = make_install(tmp_path, versions=("0.3.0.dev5+gabc",), current="0.3.0.dev5+gabc")
    py, _ = L.selected(inst["venv"], {}, windows=False, dev="")
    assert py is not None and py.parent.parent.name == "0.3.0.dev5_gabc"


@pytest.mark.parametrize("case", ["no pointer", "empty", "missing", "dev", "use-installed",
                                  "traversal", "not a string", "itself"])
def test_negative_twin_the_installer_version_runs(tmp_path, case):
    inst = make_install(tmp_path, versions=("0.2.0",), current="0.2.0")
    prefix, env, dev = inst["venv"], {}, ""
    ptr = inst["root"] / "current.json"
    if case == "no pointer":
        ptr.unlink()
    elif case == "empty":
        ptr.write_text(json.dumps({"current": "", "previous": "0.2.0"}))
    elif case == "missing":
        ptr.write_text(json.dumps({"current": "0.9.9"}))
    elif case == "dev":
        dev = "a developer install"
    elif case == "use-installed":
        env = {L.USE_INSTALLED_ENV: "1"}
    elif case == "traversal":
        ptr.write_text(json.dumps({"current": "../../venv"}))
    elif case == "not a string":
        ptr.write_text(json.dumps({"current": 3}))
    else:
        prefix = inst["root"] / "versions" / "0.2.0"          # already running 0.2.0
    py, note = L.selected(prefix, env, windows=False, dev=dev)
    assert py is None, case
    assert bool(note) == (case in ("missing", "traversal")), (case, note)
    if case == "missing":
        assert "0.9.9 is missing" in note


# --- running it ---------------------------------------------------------------------------------

def test_posix_execs_the_target_python_with_the_same_arguments(tmp_path):
    seen = []
    py = Path("/v/bin/python")
    L.run_there(py, ["update", "check", "--json"], windows=False,
                execv=lambda path, argv: seen.append((path, argv)))
    path, argv = seen[0]
    assert path == str(py)
    assert argv == [str(py), "-c", L.BOOT, "update", "check", "--json"]


def test_windows_runs_a_child_and_passes_its_exit_code(tmp_path):
    seen = []

    def call(argv):
        seen.append(argv)
        return 7

    assert L.run_there(Path("C:/v/Scripts/python.exe"), ["exit", "7"], windows=True,
                       call=call) == 7
    assert seen[0][1:3] == ["-c", L.BOOT] and seen[0][3:] == ["exit", "7"]
    # an NTSTATUS (Ctrl-C: 0xC000013A) survives sys.exit on a 32-bit C long
    assert L.run_there(Path("x"), [], windows=True, call=lambda a: 0xC000013A) == -1073741510


def _raise(*_a):
    raise OSError(8, "Exec format error")


def test_negative_twin_an_unrunnable_target_falls_back_to_the_installed_version(
        monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(L, "run_here", lambda args: ran.append(args) or 3)
    gone = Path("/gone/bin/python")
    assert L.run_there(gone, ["version"], windows=False, execv=_raise) == 3
    assert ran == [["version"]]
    assert f"cannot run {gone}" in capsys.readouterr().err

    def call_raises(argv):
        raise OSError(2, "No such file")

    assert L.run_there(Path("C:/gone/python.exe"), ["x"], windows=True, call=call_raises) == 3


def test_main_passes_the_exit_code_through_in_process(monkeypatch):
    import harness_manager.cli.main as cli

    monkeypatch.setattr(cli, "main", lambda args: 5 if args == ["boom"] else 0)
    assert L.main(["boom"]) == 5                  # this checkout is a dev install: in-process
    assert L.main(["fine"]) == 0


def test_the_boot_line_drops_the_current_directory(tmp_path):
    # A checkout in the current directory must not shadow the target venv's harness_manager.
    import subprocess

    fake = tmp_path / "harness_manager" / "cli"
    fake.mkdir(parents=True)
    (tmp_path / "harness_manager" / "__init__.py").write_text("")
    (fake / "__init__.py").write_text("")
    (fake / "main.py").write_text("def main():\n    print('SHADOWED')\n    return 9\n")
    res = subprocess.run([sys.executable, "-c", L.BOOT, "version"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)
    assert "SHADOWED" not in res.stdout
    assert res.returncode == 0 and res.stdout.strip() == __import__("harness_manager").__version__
    # negative twin: the same line without the fix picks the shadow up
    naive = "from harness_manager.cli.main import main; import sys; sys.exit(main())"
    res = subprocess.run([sys.executable, "-c", naive], cwd=tmp_path, capture_output=True,
                         text=True, timeout=120)
    assert "SHADOWED" in res.stdout and res.returncode == 9


# --- the installer's side: M2, M3, M4 ----------------------------------------------------------

@pytest.mark.parametrize("a, b", [("0.1.0", "0.1.1"), ("0.2.0rc1", "0.2.0"), ("0.2.0.dev3", "0.2.0a1"),
                                  ("0.9", "0.10"), ("1.0", "1.0.1"), ("v1.2", "1.3")])
def test_version_order(a, b):
    assert L.version_key(a) < L.version_key(b)
    assert L.version_key(b) > L.version_key(a)


def test_negative_twin_version_order_equal_and_unparsable():
    assert L.version_key("1.0") == L.version_key("1.0.0") == L.version_key("1.0+local")
    assert L.version_key("not a version") is None and L.version_key("") is None


def test_register_records_the_install_and_the_installer_venv(tmp_path):
    root, venv = tmp_path / "root", tmp_path / "root" / "venv"
    lines = L.register(root, venv, "0.1.0", extras=["serial"], uv=str(venv / "bin" / "uv"),
                       installer="install.sh")
    assert lines == []
    info = json.loads((root / "install.json").read_text())
    assert info["venv"] == str(venv) and info["version"] == "0.1.0"
    assert info["extras"] == ["serial"] and info["uv"].endswith("uv")
    ptr = json.loads((root / "current.json").read_text())
    assert ptr["current"] == "" and ptr["installer"] == {"version": "0.1.0", "venv": str(venv)}
    # a re-run keeps the extras an earlier run installed (the installer never removes them)
    L.register(root, venv, "0.1.0", extras=["app"])
    assert json.loads((root / "install.json").read_text())["extras"] == ["app", "serial"]


@pytest.mark.parametrize("current, installed, runs, previous", [
    ("0.1.1", "0.2.0", "", "0.1.1"),      # M3: newer install wins, the old one is a rollback target
    ("0.2.0", "0.2.0", "", "0.2.0"),      # at least as new wins
    ("0.3.0", "0.2.0", "0.3.0", ""),      # twin: a newer self-update keeps running; rollback -> installed
    ("0.9.9", "0.2.0", "", "0.1.0"),      # its venv is gone: the installer runs, previous untouched
])
def test_rerun_of_the_installer_and_the_pointer(tmp_path, current, installed, runs, previous):
    inst = make_install(tmp_path, versions=("0.1.1", "0.2.0", "0.3.0"))
    (inst["root"] / "current.json").write_text(json.dumps(
        {"current": current, "previous": "0.1.0", "versions": {"0.1.1": {"state": "staged"}},
         "switched_at": 1}))
    lines = L.register(inst["root"], inst["venv"], installed, extras=[], windows=False)
    ptr = json.loads((inst["root"] / "current.json").read_text())
    assert (ptr["current"], ptr["previous"]) == (runs, previous)
    assert ptr["versions"] == {"0.1.1": {"state": "staged"}} and ptr["switched_at"] == 1
    assert len(lines) == 1 and lines[0].startswith("pointer  ")


def test_migrate_moves_the_old_state_dir_layout_into_the_install_root(tmp_path):
    legacy = tmp_path / "state" / "update" / "app"
    (legacy / "versions" / "0.2.0" / "bin").mkdir(parents=True)
    (legacy / "versions" / "0.2.0" / "bin" / "python").write_text("py")
    (legacy / "wheels").mkdir()
    (legacy / "wheels" / "harness_manager-0.2.0-py3-none-any.whl").write_text("w")
    (legacy / "reqs").mkdir()
    (legacy / "reqs" / "0.2.0.txt").write_text("r")
    (legacy / "current.json").write_text(json.dumps({"current": "0.2.0", "previous": ""}))
    root = tmp_path / "root"
    lines = L.migrate(legacy, root)
    assert (root / "versions" / "0.2.0" / "bin" / "python").read_text() == "py"
    assert (root / "wheels" / "harness_manager-0.2.0-py3-none-any.whl").exists()
    assert (root / "reqs" / "0.2.0.txt").exists()
    assert json.loads((root / "current.json").read_text())["current"] == "0.2.0"
    assert not legacy.exists() and (tmp_path / "state" / "update").is_dir()
    assert any("0.2.0" in ln for ln in lines) and any("pointer" in ln for ln in lines)


def test_negative_twin_migrate_never_overwrites_the_install_root(tmp_path):
    legacy = tmp_path / "state" / "update" / "app"
    (legacy / "versions" / "0.2.0").mkdir(parents=True)
    (legacy / "versions" / "0.2.0" / "marker").write_text("legacy")
    (legacy / "current.json").write_text(json.dumps({"current": "0.2.0"}))
    root = tmp_path / "root"
    (root / "versions" / "0.2.0").mkdir(parents=True)
    (root / "versions" / "0.2.0" / "marker").write_text("root")
    (root / "current.json").write_text(json.dumps({"current": ""}))
    assert L.migrate(legacy, root) == []
    assert (root / "versions" / "0.2.0" / "marker").read_text() == "root"
    assert json.loads((root / "current.json").read_text())["current"] == ""
    assert (legacy / "versions" / "0.2.0" / "marker").read_text() == "legacy"   # left, not lost
    assert not (legacy / "current.json").exists()          # the stale pointer goes
    assert L.migrate(tmp_path / "nothing", root) == []     # no older install: nothing to do
