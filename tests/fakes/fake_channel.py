"""A fake update channel: a local HTTP server, test keys, and a builder for signed channels.

``FakeChannelServer`` serves a directory on ``127.0.0.1`` (an OS-assigned
port) the way GitHub raw + release assets do, with the behaviours the update
client must survive, each switchable per test:

- ``Range`` requests answered with 206 (resume), or ignored (``ignore_range``: 200);
- a transfer cut short once (``truncate_once[path] = n``: the headers promise the
  whole file, the body stops after ``n`` bytes and the socket closes);
- private assets under ``private/`` that need ``Authorization: Bearer <token>``
  (401 without it), like the Arm-IP release repo;
- redirects (``redirect[path] = url``) so a test can prove the token is not
  forwarded to the redirect target;
- every request recorded (``requests``): path, range, whether an Authorization
  header was sent.

``ChannelBuilder`` writes ``channel/<name>/channel.json`` (+ ``.minisig``), and
the assets it points at (relative URLs), from a list of harness and app
releases. ``TestKeys`` holds deterministic minisign keys: a release key, an
app-ci key, a root key and a rogue key the app does not trust.

No test ever reaches beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from socharness.services.update import minisign
from socharness.services.update.trust import (
    ROLE_APP_CI,
    ROLE_RELEASE,
    ROLE_ROOT,
    TrustedKey,
    TrustStore,
)

# --- keys ------------------------------------------------------------------------------


def _key(n: int) -> minisign.SecretKey:
    return minisign.SecretKey.from_seed(bytes([n]) * 32, key_id=bytes([0xA0 + n]) * 8)


@dataclass(frozen=True)
class TestKeys:
    __test__ = False                      # not a pytest class

    release: minisign.SecretKey = field(default_factory=lambda: _key(1))
    app_ci: minisign.SecretKey = field(default_factory=lambda: _key(2))
    root: minisign.SecretKey = field(default_factory=lambda: _key(3))
    rogue: minisign.SecretKey = field(default_factory=lambda: _key(4))
    rotated: minisign.SecretKey = field(default_factory=lambda: _key(5))

    def trust(self) -> TrustStore:
        """What a test app pins: release (stable/beta/dev), app-ci (dev), root."""
        return TrustStore(pinned=(
            TrustedKey(self.release.public, ROLE_RELEASE, ("stable", "beta", "dev"), "test release"),
            TrustedKey(self.app_ci.public, ROLE_APP_CI, ("dev",), "test app-ci"),
            TrustedKey(self.root.public, ROLE_ROOT, (), "test root"),
        ))


# --- the server ------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt: str, *args: Any) -> None:   # keep test output quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        srv = self.server
        path = self.path.split("?", 1)[0].lstrip("/")
        auth = self.headers.get("Authorization")
        rng = self.headers.get("Range")
        srv.owner.requests.append({"path": path, "range": rng, "auth": auth is not None,
                                   "auth_value_ok": auth == f"Bearer {srv.owner.token}"})
        if path in srv.owner.redirect:
            self.send_response(302)
            self.send_header("Location", srv.owner.redirect[path])
            self.end_headers()
            return
        if path.startswith("private/") and auth != f"Bearer {srv.owner.token}":
            self.send_response(401)
            self.end_headers()
            return
        file = (srv.owner.root / path).resolve()
        if not str(file).startswith(str(srv.owner.root.resolve())) or not file.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = file.read_bytes()
        start = 0
        if rng and not srv.owner.ignore_range and rng.startswith("bytes="):
            start = int(rng[len("bytes="):].split("-", 1)[0])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        cut = srv.owner.truncate_once.pop(path, None)
        if cut is not None:
            self.wfile.write(body[:cut])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    owner: FakeChannelServer


class FakeChannelServer:
    def __init__(self, root: Path, *, token: str = "ghp_test_token_never_log_me") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.requests: list[dict[str, Any]] = []
        self.truncate_once: dict[str, int] = {}
        self.redirect: dict[str, str] = {}
        self.ignore_range = False
        self._srv: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        assert self._srv is not None
        return f"http://127.0.0.1:{self._srv.server_address[1]}/"

    def url(self, path: str) -> str:
        return self.base + path.lstrip("/")

    def channel_url(self, name: str = "stable") -> str:
        return self.url(f"channel/{name}/channel.json")

    def source(self) -> str:
        """A ``--source`` template for ChannelClient: ``.../channel/{channel}/channel.json``."""
        return self.base + "channel/{channel}/channel.json"

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def __enter__(self) -> FakeChannelServer:
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.owner = self
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


# --- the channel builder ---------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class AssetFile:
    name: str
    data: bytes
    private: bool = False

    @property
    def rel(self) -> str:
        return f"{'private' if self.private else 'assets'}/{self.name}"


class ChannelBuilder:
    """Assemble and sign a channel directory under ``root`` (the server's root)."""

    def __init__(self, root: Path, keys: TestKeys | None = None) -> None:
        self.root = Path(root)
        self.keys = keys or TestKeys()
        self.harness: list[dict[str, Any]] = []
        self.app: list[dict[str, Any]] = []
        self.harness_current = ""
        self.app_current = ""
        self.board = {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]}
        self.extra: dict[str, Any] = {}

    def put(self, asset: AssetFile) -> dict[str, Any]:
        """Write an asset file; return its channel entry fields (url relative to the channel)."""
        path = self.root / asset.rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(asset.data)
        return {"url": f"../../{asset.rel}", "sha256": sha256_bytes(asset.data),
                "size": len(asset.data), "name": asset.name}

    def component(self, name: str, target: str, asset: AssetFile, **fields: Any) -> dict[str, Any]:
        entry = {**self.put(asset), **fields, "name": name, "target": target}
        if asset.private:
            entry.setdefault("access", "github-token")
            entry.setdefault("repo", "SoC-Labs/mps3-platform-dist-aaa")
        return entry

    def add_harness(self, version: str, identity: dict[str, Any], components: list[dict[str, Any]],
                    *, status: str = "current", rekey: bool = False,
                    compat: dict[str, Any] | None = None, current: bool = True,
                    **extra: Any) -> dict[str, Any]:
        rel = {"version": version, "status": status, "identity": identity,
               "compat": compat if compat is not None else {"min_app": "0.0.1",
                                                            "board_revs": ["HBI0309C"],
                                                            "mcc_fw_tested": ["1.3.2"]},
               "rekey": rekey, "components": components, **extra}
        self.harness.insert(0, rel)
        if current:
            for other in self.harness[1:]:
                if other["status"] == "current":
                    other["status"] = "superseded"
            self.harness_current = version
        return rel

    def add_app(self, version: str, wheel: AssetFile, *, lock: AssetFile | None = None,
                status: str = "current", current: bool = True, **extra: Any) -> dict[str, Any]:
        rel: dict[str, Any] = {"version": version, "status": status,
                               "artifacts": [{"kind": "wheel", **self.put(wheel)}], **extra}
        if lock is not None:
            rel["lock"] = self.put(lock)
        self.app.insert(0, rel)
        if current:
            for other in self.app[1:]:
                if other["status"] == "current":
                    other["status"] = "superseded"
            self.app_current = version
        return rel

    def document(self, *, channel: str, serial: int, key: minisign.SecretKey,
                 expires_at: str = "2099-01-01T00:00:00Z") -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema": "socharness-channel", "schema_version": 1, "channel": channel,
            "serial": serial, "issued_at": "2026-09-23T12:00:00Z", "expires_at": expires_at,
            "signing_key_id": key.public.id_hex, "board": dict(self.board), **self.extra,
        }
        if self.harness:
            doc["harness"] = {"current": self.harness_current, "releases": self.harness}
        if self.app:
            doc["app"] = {"current": self.app_current, "releases": self.app}
        return doc

    def publish(self, *, channel: str = "stable", serial: int = 1,
                key: minisign.SecretKey | None = None, doc: dict[str, Any] | None = None,
                expires_at: str = "2099-01-01T00:00:00Z", raw: bytes | None = None) -> Path:
        """Write and sign ``channel/<channel>/channel.json``. Returns its path."""
        key = key or self.keys.release
        data = raw if raw is not None else json.dumps(
            doc if doc is not None else self.document(channel=channel, serial=serial, key=key,
                                                      expires_at=expires_at),
            indent=1, sort_keys=True).encode()
        path = self.root / "channel" / channel / "channel.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sig = minisign.sign(data, key, trusted_comment="timestamp:1758628800\tfile:channel.json")
        path.with_name("channel.json.minisig").write_text(sig)
        return path

    def publish_keys(self, keys: list[dict[str, Any]], *, serial: int = 1,
                     revoked: list[str] | None = None, key: minisign.SecretKey | None = None,
                     channel: str = "stable") -> Path:
        data = json.dumps({"schema": "socharness-keys", "schema_version": 1, "serial": serial,
                           "keys": keys, "revoked": revoked or []}, indent=1).encode()
        path = self.root / "channel" / channel / "keys.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.with_name("keys.json.minisig").write_text(
            minisign.sign(data, key or self.keys.root, trusted_comment="keys rotation"))
        return path
