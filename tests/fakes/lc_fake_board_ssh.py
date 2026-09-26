"""A fake one-shot ``ssh`` to a Linux harness (lane LINUX-CLAIM): the board's dropbear, modelled.

``FakeBoardSsh`` stands in for ``harness_manager_mps3.claim.DEFAULT_RUN`` (``(argv, timeout) ->
RunResult``), the way ``tests/fakes/l1_fake_ssh.py`` stands in for the tunnel's launcher. Given
the argv the claim builds, it does what OpenSSH + dropbear do, because the claim depends on each:

- **host key checking**, keyed by ``HostKeyAlias`` in ``UserKnownHostsFile``:
  ``accept-new`` writes the board's key there when the alias is unknown; ``yes`` refuses an
  unknown key ("No ED25519 host key is known ... Host key verification failed.") and a known
  but different one (the "REMOTE HOST IDENTIFICATION HAS CHANGED" banner), status 255;
- **key-only auth**: the key ``-i`` names (its ``.pub`` beside it) must be in the board's
  ``authorized_keys`` (the FakeShell's TOFU claim, ``shell.authorized_keys``); otherwise
  "Permission denied (publickey)." and status 255;
- ``mitm_key``: a different host key is offered than the one the board's identify publishes
  (something else answers for the board).

Every argv is kept in ``calls``. Nothing is run and nothing touches ``~/.ssh``.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness_manager_mps3.claim import RunResult


def make_key_line(seed: str, kind: str = "ssh-ed25519") -> str:
    """A syntactically real OpenSSH public key line (the blob is not a curve point: nothing
    here verifies a signature, only fingerprints)."""
    name = kind.encode()
    blob = len(name).to_bytes(4, "big") + name + (32).to_bytes(4, "big") + \
        hashlib.sha256(seed.encode()).digest()
    return f"{kind} {base64.b64encode(blob).decode()}"


def write_key_pair(directory: Path, name: str, comment: str = "you@test") -> tuple[Path, Path]:
    """``(private, public)`` files: the private one is a placeholder (never read by HM)."""
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / name
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nplaceholder\n"
                       "-----END OPENSSH PRIVATE KEY-----\n")
    os.chmod(private, 0o600)
    public = directory / f"{name}.pub"
    public.write_text(f"{make_key_line(name)} {comment}\n")
    return private, public


def _opts(argv: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(argv[:-1]):
        if a == "-o":
            k, _, v = argv[i + 1].partition("=")
            out.setdefault(k.lower(), v)
    return out


class FakeBoardSsh:
    def __init__(self, shell: Any, host_key_line: str, *, mitm_key: str = "") -> None:
        self.shell = shell                    # a pyverify FakeShell (its authorized_keys)
        self.host_key_line = host_key_line
        self.mitm_key = mitm_key
        self.calls: list[list[str]] = []

    def offered(self) -> str:
        return self.mitm_key or self.host_key_line

    def __call__(self, argv: Sequence[str], timeout: float) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        o = _opts(argv)
        alias = o.get("hostkeyalias") or argv[-2]
        kh = Path(o.get("userknownhostsfile", "/nonexistent"))
        mode = o.get("stricthostkeychecking", "ask")
        offered = self.offered()
        known = {}
        if kh.is_file():
            for line in kh.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    known[parts[0]] = f"{parts[1]} {parts[2]}"
        have = known.get(alias)
        if have is None:
            if mode != "accept-new":
                return RunResult(255, "", f"No ED25519 host key is known for {alias} and you "
                                          "have requested strict checking.\r\n"
                                          "Host key verification failed.\r\n")
            with kh.open("a") as fh:
                fh.write(f"{alias} {offered}\n")
        elif have.split()[:2] != offered.split()[:2]:
            return RunResult(255, "", "@@@@@@@@@@@\r\n@    WARNING: REMOTE HOST IDENTIFICATION "
                                      "HAS CHANGED!     @\r\n@@@@@@@@@@@\r\n"
                                      "Host key verification failed.\r\n")
        ident = argv[argv.index("-i") + 1] if "-i" in argv else ""
        pub = Path(ident + ".pub") if ident else None
        allowed = (self.shell.authorized_keys or b"").decode()
        mine = pub.read_text().split()[:2] if pub is not None and pub.is_file() else None
        if not mine or " ".join(mine) not in allowed:
            return RunResult(255, "", "root@192.168.10.101: Permission denied (publickey).\r\n")
        return RunResult(0, "", "")
