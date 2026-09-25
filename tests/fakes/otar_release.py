"""OTA-R fixtures: a small Harness-Manager-shaped git repo, fake build and lock tools, a fake
``minisign`` CLI, and harness bundle dirs built from the HARNESS-DIST spike's fake mints.

Nothing here reaches the network or a real key: keys come from
``tools.release.signer.keygen_throwaway`` under pytest's temp dir, the wheel and the lock
are written by fakes, and ``gh`` is never run (a test that needs it records the argv).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from tests.spikes.harness_dist_publish import MintRecord
from tools.release.common import run

GIT_ENV = {"GIT_AUTHOR_NAME": "otar", "GIT_AUTHOR_EMAIL": "otar@example.invalid",
           "GIT_COMMITTER_NAME": "otar", "GIT_COMMITTER_EMAIL": "otar@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
PYVERIFY_WHEEL = "mps3_pyverify-0.1.0-py3-none-any.whl"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         env={**os.environ, **GIT_ENV}, check=True)
    return res.stdout.strip()


def fake_wheel_bytes(version: str, name: str = "harness-manager") -> bytes:
    dist = name.replace("-", "_")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{dist}/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr(f"{dist}-{version}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        zf.writestr(f"{dist}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    return buf.getvalue()


def make_repo(root: Path, version: str = "0.2.0", *, changelog_version: str | None = None,
              init_version: str | None = None, pyproject_version: str | None = None) -> Path:
    """A committed checkout with the files the release tool reads.

    ``__version__`` (``init_version or version``) is the one version source (CCR OTA-R): the
    pyproject reads it with a dynamic version, unless ``pyproject_version`` gives the old
    layout, a static version of its own (which the release tool refuses)."""
    root.mkdir(parents=True, exist_ok=True)
    head = (f'version = "{pyproject_version}"\n' if pyproject_version else
            'dynamic = ["version"]\n')
    tail = ("" if pyproject_version else
            '\n[tool.setuptools.dynamic]\nversion = { attr = "harness_manager.__version__" }\n')
    (root / "pyproject.toml").write_text(
        '[project]\nname = "harness-manager"\n'
        f'{head}requires-python = ">=3.10"\n'
        'dependencies = ["mps3-pyverify>=0.1.0", "cryptography>=42"]\n' + tail,
        encoding="utf-8")
    pkg = root / "src" / "harness_manager"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{init_version or version}"\n',
                                     encoding="utf-8")
    cv = changelog_version or version
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {cv} (2026-09-24)\n\n- Self-update from a signed channel.\n\n"
        "## 0.1.0\n\n- The first release.\n", encoding="utf-8")
    (root / "constraints.txt").write_text("cryptography==50.0.1\n", encoding="utf-8")
    (root / "vendor").mkdir(exist_ok=True)
    (root / "vendor" / PYVERIFY_WHEEL).write_bytes(fake_wheel_bytes("0.1.0", "mps3-pyverify"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"release {version}")
    return root


def commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def tag(repo: Path, name: str, ref: str = "HEAD") -> None:
    _git(repo, "tag", name, ref)


class FakeTools:
    """A runner: ``fake-build``/``fake-uv``/``fake-pip-compile`` are faked, ``gh`` is recorded
    and answered (never run), everything else (git) runs for real."""

    def __init__(self, *, raw_lock: str | None = None, gh_exists: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.raw_lock = raw_lock or (
            "cryptography==50.0.1 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "cffi==2.1.1 ; platform_python_implementation != 'PyPy' \\\n"
            "    --hash=sha256:" + "b" * 64 + "\n"
            "mps3-pyverify==0.1.0 \\\n    --hash=sha256:" + "c" * 64 + "\n")
        self.gh_exists = gh_exists

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if Path(c[0]).name in ("gh", "fake-gh")]

    def __call__(self, argv: Any, *, cwd: Path | None = None, env: dict | None = None,
                 capture: bool = True, stdin_tty: bool = False, check: bool = False,
                 ) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        prog = Path(argv[0]).name
        if prog in ("fake-uv", "fake-pip-compile"):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{prog} 9.9.9\n", "")
            out = Path(argv[argv.index("-o") + 1])
            out = out if out.is_absolute() else Path(cwd or ".") / out
            out.write_text(self.raw_lock, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if prog == "git" and "push" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")      # recorded, never pushed
        if prog in ("gh", "fake-gh"):
            if argv[1:3] == ["release", "view"]:
                return subprocess.CompletedProcess(argv, 0 if self.gh_exists else 1, "",
                                                   "" if self.gh_exists else "release not found")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 2 and argv[1:3] == ["-m", "build"]:
            out = Path(argv[argv.index("--outdir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            src = Path(argv[-1])
            version = _read_version(src)
            (out / f"harness_manager-{version}-py3-none-any.whl").write_bytes(
                fake_wheel_bytes(version))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run(argv, cwd=cwd, env=env, capture=capture, stdin_tty=False)


def _read_version(src: Path) -> str:
    """The version setuptools would build: pyproject's static one, else ``__version__``."""
    import re

    text = (src / "pyproject.toml").read_text(encoding="utf-8")
    static = re.search(r'^version = "([^"]+)"$', text, re.M)
    if static:
        return static.group(1)
    init = (src / "src" / "harness_manager" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"$', init, re.M).group(1)  # type: ignore[union-attr]


FAKE_MINISIGN = '''#!{python}
"""A stand-in for the minisign CLI (tests only): -S -s SK -m FILE -x SIG -t COMMENT -c C."""
import sys
sys.path[:0] = {paths!r}
from pathlib import Path
from harness_manager.services.update import minisign
from tools.release.signer import parse_secret_key
a = sys.argv[1:]
assert a[0] == "-S", a
opt = dict(zip(a[1::2], a[2::2]))
key = parse_secret_key(Path(opt["-s"]).read_text())
data = Path(opt["-m"]).read_bytes()
Path(opt["-x"]).write_text(minisign.sign(data, key, trusted_comment=opt["-t"],
                                         untrusted_comment=opt.get("-c", "")))
Path(opt["-x"] + ".argv").write_text(" ".join(a))
'''


def fake_minisign(directory: Path) -> Path:
    """An executable ``minisign`` that signs with HM's module (the real CLI is not installed)."""
    repo = Path(__file__).resolve().parents[2]
    path = directory / "minisign"
    path.write_text(FAKE_MINISIGN.format(python=sys.executable,
                                         paths=[str(repo / "src"), str(repo)]), encoding="utf-8")
    path.chmod(0o755)
    return path


# --- harness bundles from the HARNESS-DIST spike's fake mints ------------------------------


def _wrap(v: Any) -> dict[str, Any]:
    return {"reason": None, "value": v}


def write_bundle(root: Path, mint: MintRecord, *, dirty: bool = False,
                 linux: dict[str, Any] | None = None, extra_sd: dict[str, bytes] | None = None,
                 extra_open: dict[str, bytes] | None = None,
                 mint_usercode: str | None = None) -> Path:
    """A bundle dir (tools/release/harness.py's input contract) for one fake mint."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in {**mint.sd_files, **(extra_sd or {})}.items():
        p = root / "sd" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for cls, files in (("open", {**mint.overlays_open, **(extra_open or {})}),
                       ("aaa", mint.overlays_aaa)):
        for rel, data in files.items():
            p = root / "overlays" / cls / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    (root / "firmware.json").write_text(json.dumps({
        "version": mint.wire_harness, "sha": mint.fw_sha, "dirty": False, "proto": mint.proto,
        "features": list(mint.features)}), encoding="utf-8")
    (root / "notes.md").write_text(f"Fake mint {mint.static_id} (fw {mint.fw_sha}).\n",
                                   encoding="utf-8")
    if mint.impl == "linux":
        import hashlib

        bit = mint.sd_files["MB/HBI0309C/Nanosoc/nanosoc.bit"]
        (root / "linux_slot.img").write_bytes(mint.os_image)
        legal = b"legal-info tar (fake)\n"
        (root / "linux_legal_info.tar").write_bytes(legal)
        doc = {"schema": "mps3-linux-bundle", "schema_version": 1, "mint_kind": "mint",
               "fieldable": True, "shell_cpu": "mbv", "static_id": mint.static_id,
               "static_usercode": mint.usercode, "static_ver32": mint.ver32 or "0x01000000",
               "targets": {
                   "mcc_sd": {"flashable_bit": {"name": "config_rm_greybox_stage0.bit",
                                                "sha256": hashlib.sha256(bit).hexdigest(),
                                                "bytes": len(bit)}},
                   "ethernet": {
                       "slot_image": {"name": "linux_slot.img", "format": "s0lb",
                                      "sha256": hashlib.sha256(mint.os_image).hexdigest(),
                                      "bytes": len(mint.os_image)},
                       "provisioned": {"static_id": mint.static_id},
                       "components": {"harnessd_sha256": mint.fw_sha + "0" * 56,
                                      "image_kind": "release", "ver32": "0x01000000"},
                       "legal_info": {"name": "linux_legal_info.tar",
                                      "sha256": hashlib.sha256(legal).hexdigest(),
                                      "bytes": len(legal)}}}}
        _deep_update(doc, linux or {})
        (root / "linux_bundle.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    else:
        (root / "mint.json").write_text(json.dumps({
            "schema": "mps3-mint-record", "schema_version": "1.1", "record_kind": "mint",
            "static_id": mint.static_id,
            "static_usercode": _wrap({"usercode": mint_usercode or mint.usercode,
                                      "usr_access": _wrap(mint.ver32),
                                      "harness_version": _wrap(mint.wire_harness)}),
            "sources": {"repo": _wrap({"sha": "c85510817ae7d9cacc6533c1e2e37fadb0fd60bb",
                                       "dirty": dirty})},
            "tools": {"vivado": _wrap(mint.vivado or "2024.1")}}, indent=1), encoding="utf-8")
    return root


def _deep_update(doc: dict[str, Any], upd: dict[str, Any]) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            _deep_update(doc[k], v)
        else:
            doc[k] = v
