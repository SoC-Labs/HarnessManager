"""HM pins the platform's PUBLISHED pyverify wheel (``pyverify.pin``): version, sha256, url.

- one pin file is the only place that names the version and the sha256; pyproject agrees;
- the resolver (``scripts/pyverify_pin.sh``, sourced by install.sh and make_wheelhouse.sh) takes
  a wheelhouse wheel or a ``vendor/`` wheel only when its sha256 IS the pin's, else downloads
  the pin's url and checks the sha256: a mismatch or a failed download is refused, with the url
  and both hashes in the message, and nothing is installed;
- the release tool refuses a ``dep`` wheel whose sha256 differs from the pin;
- ``scripts/pin_pyverify.sh <version> <sha256>`` rewrites the pin and pyproject together.

No test here reaches the network: downloads go to a local http.server on a port in 24000-32000.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from tools.release.app import pinned_dep, read_pyverify_pin
from tools.release.common import ReleaseError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the pin scripts are bash")

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "pyverify_pin.sh"
PIN = ROOT / "pyverify.pin"
NAME = "mps3_pyverify-1.2.3-py3-none-any.whl"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pin_file(dest: Path, data: bytes, url: str = "https://example.invalid/x.whl") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    pin = dest / "pyverify.pin"
    pin.write_text(f"# test\nversion=1.2.3\nsha256={_sha(data)}\nurl={url}\n")
    return pin


def _resolve(pin: Path, work: Path, offline: str, *vendor: Path, url: str = ""):
    """Run pyverify_resolve in bash; returns (returncode, wheel path, from, stderr)."""
    script = (
        'die() { printf "die: %s\\n" "$*" >&2; exit 1; }\n'
        f'. "{LIB}"\n'
        'pyverify_wheel="" pyverify_from=""\n'
        'pyverify_resolve "$@"\n'
        'printf "%s\\n%s\\n" "$pyverify_wheel" "$pyverify_from"\n')
    env = {"PATH": os.environ["PATH"], "HOME": str(work)}
    if url:
        env["HM_PYVERIFY_URL"] = url
    r = subprocess.run(["bash", "-c", script, "x", str(pin), str(work / "dl"), offline,
                        *map(str, vendor)], capture_output=True, text=True, env=env, timeout=60)
    out = r.stdout.splitlines()
    return r.returncode, (out[0] if out else ""), (out[1] if len(out) > 1 else ""), r.stderr


def _free_port() -> int:
    for port in range(24100, 32000, 7):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 24000-32000")


@pytest.fixture
def server(tmp_path):
    """A local http.server over ``tmp_path/www``; yields (base url, www dir)."""
    www = tmp_path / "www"
    www.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(www), **k)

        def log_message(self, *a):
            pass

    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", www
    srv.shutdown()
    srv.server_close()


# -- the pin file is the single source ----------------------------------------------------------

def _read_pin() -> dict[str, str]:
    return read_pyverify_pin(ROOT)


def test_the_pin_is_well_formed_and_its_url_follows_its_version():
    pin = _read_pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["version"])
    assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
    assert pin["url"] == (
        "https://github.com/SoC-Labs/MPS3-NanoSoC-Verification-Platform/releases/download/"
        f"v{pin['version']}/mps3_pyverify-{pin['version']}-py3-none-any.whl")


def test_pyproject_depends_on_exactly_the_pinned_version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"mps3-pyverify=={_read_pin()["version"]}"' in text
    assert subprocess.run([str(ROOT / "scripts" / "pin_pyverify.sh"), "--check"],
                          capture_output=True).returncode == 0


def test_nothing_else_hard_codes_the_pinned_sha256():
    sha = _read_pin()["sha256"]
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    allowed = {"pyverify.pin", "vendor/README.md"}      # the pin; the vendor wheel's provenance
    hits = []
    for rel in files.splitlines():
        if rel in allowed or rel.startswith("docs/evidence/") or rel.endswith(".whl"):
            continue
        p = ROOT / rel
        if p.is_file() and sha.encode() in p.read_bytes():
            hits.append(rel)
    assert not hits, f"the pinned sha256 is hard-coded outside pyverify.pin: {hits}"


def test_the_pin_script_is_the_only_writer_of_the_dependency_pin():
    cons = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert "mps3-pyverify==" not in cons            # installed by path from the pin, never locked here


# -- resolution order and refusals ---------------------------------------------------------------

def test_wheelhouse_wheel_with_the_pinned_sha_is_used_with_no_network(tmp_path):
    data = b"the wheel"
    wh = tmp_path / "wh"
    wh.mkdir()
    (wh / NAME).write_bytes(data)
    pin = _pin_file(tmp_path / "pin", data, url="http://127.0.0.1:9/never")
    rc, wheel, src, err = _resolve(pin, tmp_path, str(wh), url="http://127.0.0.1:9/never")
    assert rc == 0, err
    assert wheel == str(wh / NAME) and "wheelhouse" in src


def test_twin_wheelhouse_wheel_with_another_sha_is_refused_and_never_downloaded(tmp_path):
    wh = tmp_path / "wh"
    wh.mkdir()
    (wh / NAME).write_bytes(b"some other build")
    pin = _pin_file(tmp_path / "pin", b"the wheel")
    rc, wheel, _, err = _resolve(pin, tmp_path, str(wh), url="http://127.0.0.1:9/never")
    assert rc == 1 and not wheel
    assert _sha(b"the wheel") in err and "wheelhouse" in err and "never" not in err


def test_vendor_wheel_is_used_only_when_its_sha_equals_the_pin(tmp_path, server):
    base, www = server
    data = b"the wheel"
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / NAME).write_bytes(data)
    pin = _pin_file(tmp_path / "pin", data)
    rc, wheel, src, err = _resolve(pin, tmp_path, "", vendor, url="http://127.0.0.1:9/never")
    assert rc == 0, err
    assert wheel == str(vendor / NAME) and src == str(vendor)
    # twin: a vendor wheel with another sha is ignored; the pin's url is tried (and verified)
    (vendor / NAME).write_bytes(b"stale vendor wheel")
    (www / NAME).write_bytes(data)
    rc, wheel, src, err = _resolve(pin, tmp_path, "", vendor, url=f"{base}/{NAME}")
    assert rc == 0, err
    assert wheel.startswith(str(tmp_path / "dl")) and src == f"{base}/{NAME}"
    assert Path(wheel).read_bytes() == data


def test_download_with_the_right_sha_is_accepted(tmp_path, server):
    base, www = server
    (www / NAME).write_bytes(b"published")
    pin = _pin_file(tmp_path / "pin", b"published")
    rc, wheel, _, err = _resolve(pin, tmp_path, "", url=f"{base}/{NAME}")
    assert rc == 0, err
    assert _sha(Path(wheel).read_bytes()) == _sha(b"published")


def test_twin_download_with_another_sha_is_refused_naming_url_and_both_hashes(tmp_path, server):
    base, www = server
    (www / NAME).write_bytes(b"tampered")
    pin = _pin_file(tmp_path / "pin", b"published")
    rc, wheel, _, err = _resolve(pin, tmp_path, "", url=f"{base}/{NAME}")
    assert rc == 1 and not wheel
    assert f"{base}/{NAME}" in err
    assert _sha(b"tampered") in err and _sha(b"published") in err and "NOT installed" in err
    assert not list((tmp_path / "dl").glob("*.whl")), "the unverified file was left behind"


def test_twin_download_failure_is_refused_naming_the_url(tmp_path):
    pin = _pin_file(tmp_path / "pin", b"published")
    url = f"http://127.0.0.1:{_free_port()}/{NAME}"          # nothing listens there
    rc, wheel, _, err = _resolve(pin, tmp_path, "", url=url)
    assert rc == 1 and not wheel
    assert url in err and _sha(b"published") in err and "Nothing was installed" in err


def test_twin_download_404_is_refused_naming_the_url(tmp_path, server):
    base, _ = server
    pin = _pin_file(tmp_path / "pin", b"published")
    rc, _, _, err = _resolve(pin, tmp_path, "", url=f"{base}/{NAME}")
    assert rc == 1 and f"{base}/{NAME}" in err


def test_twin_a_pin_without_a_valid_sha_is_refused(tmp_path):
    pin = tmp_path / "pyverify.pin"
    pin.write_text("version=1.2.3\nsha256=PLACEHOLDER\nurl=https://example.invalid/x.whl\n")
    rc, _, _, err = _resolve(pin, tmp_path, "")
    assert rc == 1 and "no valid sha256" in err


# -- the release tool ---------------------------------------------------------------------------

def _tree(root: Path, wheel_data: bytes, pin_data: bytes) -> Path:
    (root / "vendor").mkdir(parents=True)
    (root / "vendor" / "mps3_pyverify-0.1.0-py3-none-any.whl").write_bytes(wheel_data)
    (root / "pyverify.pin").write_text(f"version=0.1.0\nsha256={_sha(pin_data)}\nurl=x\n")
    return root


def test_release_attaches_the_wheel_the_pin_names(tmp_path):
    src = _tree(tmp_path / "src", b"w", b"w")
    assert pinned_dep(src).name == "mps3_pyverify-0.1.0-py3-none-any.whl"


def test_twin_release_refuses_a_vendor_wheel_whose_sha_differs_from_the_pin(tmp_path):
    src = _tree(tmp_path / "src", b"not the pinned wheel", b"the pinned wheel")
    with pytest.raises(ReleaseError, match="does not match pyverify.pin") as ei:
        pinned_dep(src)
    assert _sha(b"the pinned wheel") in str(ei.value) and _sha(b"not the pinned wheel") in str(ei.value)


def test_release_takes_the_published_wheel_when_vendor_is_gone(tmp_path):
    src = _tree(tmp_path / "src", b"w", b"published")
    shutil.rmtree(src / "vendor")
    with pytest.raises(ReleaseError, match="--pyverify-wheel"):
        pinned_dep(src)
    got = tmp_path / "mps3_pyverify-0.1.0-py3-none-any.whl"
    got.write_bytes(b"published")
    assert pinned_dep(src, got) == got
    got.write_bytes(b"swapped")
    with pytest.raises(ReleaseError, match="does not match pyverify.pin"):
        pinned_dep(src, got)


# -- scripts/pin_pyverify.sh --------------------------------------------------------------------

def test_pin_script_rewrites_pin_and_pyproject_together(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "vendor").mkdir()
    for f in ("pyverify.pin", "pyproject.toml"):
        shutil.copy(ROOT / f, repo / f)
    shutil.copy(ROOT / "scripts" / "pin_pyverify.sh", repo / "scripts")
    shutil.copy(LIB, repo / "scripts")
    sha = "ab" * 32
    script = str(repo / "scripts" / "pin_pyverify.sh")
    r = subprocess.run([script, "2.2.1", sha], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pin = read_pyverify_pin(repo)
    assert pin["version"] == "2.2.1" and pin["sha256"] == sha
    assert pin["url"].endswith("/download/v2.2.1/mps3_pyverify-2.2.1-py3-none-any.whl")
    assert "PLACEHOLDER" not in (repo / "pyverify.pin").read_text()
    assert '"mps3-pyverify==2.2.1"' in (repo / "pyproject.toml").read_text()
    assert subprocess.run([script, "--check"], capture_output=True).returncode == 0
    # twins: refused, and nothing changed
    before = (repo / "pyverify.pin").read_text()
    for args in (["2.2", sha], ["2.2.1", "XYZ"], ["2.2.1"]):
        assert subprocess.run([script, *args], capture_output=True).returncode == 2
    assert (repo / "pyverify.pin").read_text() == before
    # --check notices a disagreement
    text = (repo / "pyproject.toml").read_text().replace("==2.2.1", "==9.9.9")
    (repo / "pyproject.toml").write_text(text)
    assert subprocess.run([script, "--check"], capture_output=True).returncode == 2
