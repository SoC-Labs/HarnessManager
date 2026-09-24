#!/usr/bin/env python3
"""Spike (lane OTA): a lead-run, local `make release` for the app, as a prototype.

NOT a product tool. It proves what a release must contain for T7's app self-update
(services/update/app.py) to install it, and it uses THROWAWAY keys only. The real
tool is lane OTA-R in docs/design/HM_SELF_UPDATE.md.

    ota_release.py keygen OUT_DIR                 a throwaway minisign key (seed + id + pub)
    ota_release.py pin SRC_DIR PUB_FILE           pin that public key in a source copy's trust.py
    ota_release.py bump SRC_DIR VERSION           set the version in pyproject.toml + __init__.py
    ota_release.py lock SRC_DIR OUT BASE_URL      a universal, hashed lock (uv), with the vendored
                                                  pyverify wheel pinned by URL + hash
    ota_release.py publish WWW CHANNEL SERIAL KEY_DIR [--app VERSION WHEEL [LOCK]]...
                                                  write assets + channel.json + .minisig

Run it with a Python that has `cryptography`, and PYTHONPATH=<checkout>/src so it can
import harness_manager.services.update.minisign (the same signer the tests use).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _minisign():
    from harness_manager.services.update import minisign
    return minisign


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def keygen(out: Path) -> None:
    ms = _minisign()
    out.mkdir(parents=True, exist_ok=True)
    key = ms.SecretKey.generate()
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    seed = key.private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    (out / "release.seed").write_bytes(seed)
    os.chmod(out / "release.seed", 0o600)
    (out / "release.id").write_text(key.key_id.hex())
    (out / "release.pub").write_text(key.public.to_base64() + "\n")
    print(f"key {key.public.id_hex} -> {out}")


def _load_key(key_dir: Path):
    ms = _minisign()
    return ms.SecretKey.from_seed((key_dir / "release.seed").read_bytes(),
                                  bytes.fromhex((key_dir / "release.id").read_text().strip()))


def pin(src: Path, pub_file: Path) -> None:
    trust = src / "src/harness_manager/services/update/trust.py"
    text = trust.read_text()
    pub = pub_file.read_text().strip()
    new = (f'PINNED_KEYS: tuple[TrustedKey, ...] = (\n'
           f'    pinned("{pub}", ROLE_RELEASE, note="OTA SPIKE THROWAWAY KEY"),\n)')
    text, n = re.subn(r"^PINNED_KEYS: tuple\[TrustedKey, \.\.\.\] = \(\)$", new, text, flags=re.M)
    if n != 1:
        sys.exit("pin: PINNED_KEYS = () not found (already pinned?)")
    trust.write_text(text)
    print(f"pinned {pub[:16]}... in {trust}")


def bump(src: Path, version: str) -> None:
    for rel, pat, rep in (("pyproject.toml", r'^version = "[^"]+"$', f'version = "{version}"'),
                          ("src/harness_manager/__init__.py", r'^__version__ = "[^"]+"$',
                           f'__version__ = "{version}"')):
        p = src / rel
        text, n = re.subn(pat, rep, p.read_text(), flags=re.M)
        if n != 1:
            sys.exit(f"bump: no version line in {p}")
        p.write_text(text)
    print(f"version {version} in {src}")


def lock(src: Path, out: Path, base_url: str) -> None:
    """``uv pip compile --universal --generate-hashes``; pyverify re-pinned by URL + hash.

    T7 installs with ``uv pip install --require-hashes -r reqs`` and NO ``--find-links``,
    so a lock line ``mps3-pyverify==0.1.0 --hash=...`` would be looked up on PyPI, where
    it does not exist. The only way to name it in a requirements file is a direct URL,
    which must be absolute: the release must know where it will be hosted.
    """
    uv = os.environ.get("HARNESS_MANAGER_UV") or shutil.which("uv") or sys.exit("lock: needs uv")
    vendor = next((src / "vendor").glob("mps3_pyverify-*.whl"))
    raw = out.with_suffix(".raw.txt")
    cmd = [uv, "pip", "compile", "--quiet", "--universal", "--generate-hashes",
           "--python-version", "3.10", "--find-links", str(src / "vendor"),
           "-c", str(src / "constraints.txt"), "--extra", "serial",
           "--no-header", "--no-annotate", "-o", str(raw), str(src / "pyproject.toml")]
    subprocess.run(cmd, check=True)
    lines, skip = [], False
    for ln in raw.read_text().splitlines():
        if ln.startswith("mps3-pyverify"):
            skip = True
            lines.append(f"mps3-pyverify @ {base_url.rstrip('/')}/{vendor.name} "
                         f"--hash=sha256:{sha256(vendor)}")
            continue
        if skip and ln.startswith("    "):
            continue                                   # the old entry's --hash lines
        skip = False
        lines.append(ln)
    out.write_text("\n".join(lines) + "\n")
    raw.unlink()
    print(f"lock {out} ({sum(1 for x in lines if x and not x.startswith(' '))} packages)")


def publish(www: Path, channel: str, serial: int, key_dir: Path,
            apps: list[tuple[str, Path, Path | None]], extra_assets: list[Path]) -> None:
    ms = _minisign()
    key = _load_key(key_dir)
    assets = www / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    def put(p: Path) -> dict:
        shutil.copyfile(p, assets / p.name)
        return {"name": p.name, "url": f"../../assets/{p.name}", "sha256": sha256(p),
                "size": p.stat().st_size}

    for p in extra_assets:
        put(p)
    releases = []
    for i, (version, wheel, lck) in enumerate(apps):
        rel = {"version": version, "status": "current" if i == 0 else "superseded",
               "requires_python": ">=3.10",
               "released_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "artifacts": [{"kind": "wheel", **put(wheel)}]}
        if lck is not None:
            rel["lock"] = put(lck)
        releases.append(rel)
    doc = {"schema": "harness-manager-channel", "schema_version": 1, "channel": channel,
           "serial": serial, "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "expires_at": "2099-01-01T00:00:00Z", "signing_key_id": key.public.id_hex,
           "board": {"pack": "mps3"},
           "app": {"current": apps[0][0], "releases": releases}}
    data = json.dumps(doc, indent=1, sort_keys=True).encode()
    path = www / "channel" / channel / "channel.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_name("channel.json.minisig").write_text(
        ms.sign(data, key, trusted_comment=f"timestamp:{int(time.time())}\tfile:channel.json"))
    print(f"published {channel} serial {serial}: app {[a[0] for a in apps]} -> {path}")


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "keygen":
        keygen(Path(rest[0]))
    elif cmd == "pin":
        pin(Path(rest[0]), Path(rest[1]))
    elif cmd == "bump":
        bump(Path(rest[0]), rest[1])
    elif cmd == "lock":
        lock(Path(rest[0]), Path(rest[1]), rest[2])
    elif cmd == "publish":
        www, channel, serial, key_dir = Path(rest[0]), rest[1], int(rest[2]), Path(rest[3])
        apps, extra, i = [], [], 4
        while i < len(rest):
            if rest[i] == "--app":
                version, wheel = rest[i + 1], Path(rest[i + 2])
                lck = None
                if i + 3 < len(rest) and not rest[i + 3].startswith("--"):
                    lck = Path(rest[i + 3]) if rest[i + 3] != "-" else None
                    i += 1
                apps.append((version, wheel, lck))
                i += 3
            elif rest[i] == "--asset":
                extra.append(Path(rest[i + 1]))
                i += 2
            else:
                sys.exit(f"publish: unknown {rest[i]}")
        publish(www, channel, serial, key_dir, apps, extra)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
