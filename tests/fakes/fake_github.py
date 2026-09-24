"""A fake of GitHub's REST API for release assets, on 127.0.0.1 (lane OTA-C).

It behaves the way the update client depends on, for a PRIVATE repo (david U1):

- ``GET /repos/{o}/{r}/releases/tags/{tag}``: the release JSON; its ``assets`` list holds
  at most ``embed`` assets (the rest only through ``assets_url``, paged, as the client
  must handle); every asset's ``url`` is ``{api}/repos/{o}/{r}/releases/assets/{id}``;
- ``GET /repos/{o}/{r}/releases/{rid}/assets?per_page=&page=``: one page of assets;
- ``GET /repos/{o}/{r}/releases/assets/{id}``: with ``Accept: application/octet-stream``
  a **302 to signed storage** (``/storage/{id}?X-Amz-Signature=...``); otherwise the
  asset's JSON;
- ``GET /storage/{id}``: the bytes, with ``Range`` (206). It records whether an
  ``Authorization`` header arrived: the client must NEVER send the token there;
- a private repo answers **404** to a request without the right token (GitHub does not
  say "401" for a private repo it hides), and 401 to a wrong one;
- ``bogus_asset_url``: the release JSON names an asset URL outside the API (the client
  must refuse it and never send the token there).

``publish_tree(root)`` loads a release-tool tree (``tools/release`` Layout: GitHub's URL
layout, ``<root>/<owner>/<repo>/releases/download/<tag>/<file>``) as releases.
Nothing here reaches beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj: Any) -> None:
        self._send(200, json.dumps(obj).encode(), {"Content-Type": "application/json"})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        gh = self.server.owner
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        auth = self.headers.get("Authorization")
        gh.requests.append({"path": path, "query": parts.query, "auth": auth is not None,
                            "auth_ok": auth == f"Bearer {gh.token}",
                            "accept": self.headers.get("Accept", ""),
                            "range": self.headers.get("Range")})
        seg = [s for s in path.split("/") if s]
        if seg[:1] == ["storage"] and len(seg) == 2:
            gh.storage_hits.append({"id": seg[1], "auth": auth is not None})
            return self._storage(int(seg[1]))
        if seg[:1] != ["repos"] or len(seg) < 5 or seg[3] != "releases":
            return self._send(404)
        owner_repo = f"{seg[1]}/{seg[2]}"
        if gh.private and auth != f"Bearer {gh.token}":
            return self._send(401 if auth else 404,
                              b'{"message": "Bad credentials"}' if auth else b'{"message": "Not Found"}')
        if seg[4] == "tags" and len(seg) == 6:
            rel = gh.release(owner_repo, seg[5])
            return self._json(rel) if rel is not None else self._send(404)
        if seg[4] == "assets" and len(seg) == 6:
            asset = gh.asset(int(seg[5]))
            if asset is None or asset["repo"] != owner_repo:
                return self._send(404)
            if "application/octet-stream" in self.headers.get("Accept", ""):
                loc = f"{gh.api}/storage/{asset['id']}?X-Amz-Signature=deadbeef"
                return self._send(302, b"", {"Location": loc})
            return self._json(gh.asset_json(asset))
        if len(seg) == 6 and seg[5] == "assets":
            q = parse_qs(parts.query)
            per = int(q.get("per_page", ["30"])[0])
            page = int(q.get("page", ["1"])[0])
            assets = gh.assets_of(owner_repo, int(seg[4]))
            return self._json([gh.asset_json(a) for a in assets[(page - 1) * per:page * per]])
        return self._send(404)

    def _storage(self, aid: int) -> None:
        gh = self.server.owner
        asset = gh.asset(aid)
        if asset is None:
            return self._send(404)
        data: bytes = asset["data"]
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-", 1)[0])
            return self._send(206, data[start:],
                              {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return self._send(200, data)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    owner: FakeGitHub


class FakeGitHub:
    def __init__(self, *, token: str = "ghp_fake_github_token_never_log_me", private: bool = True,
                 embed: int = 30) -> None:
        self.token = token
        self.private = private
        self.embed = embed
        self.bogus_asset_url = False
        self.requests: list[dict[str, Any]] = []
        self.storage_hits: list[dict[str, Any]] = []
        self._releases: dict[tuple[str, str], dict[str, Any]] = {}   # (repo, tag) -> release
        self._assets: dict[int, dict[str, Any]] = {}
        self._next = 1000
        self._srv: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- content --

    def add(self, owner_repo: str, tag: str, name: str, data: bytes) -> int:
        rel = self._releases.setdefault((owner_repo, tag), {"id": len(self._releases) + 1,
                                                            "tag": tag, "assets": []})
        rel["assets"] = [a for a in rel["assets"] if self._assets[a]["name"] != name]
        self._next += 1
        self._assets[self._next] = {"id": self._next, "name": name, "data": data,
                                    "repo": owner_repo, "tag": tag}
        rel["assets"].append(self._next)
        return self._next

    def publish_tree(self, root: Path) -> int:
        """Every ``<root>/<owner>/<repo>/releases/download/<tag>/<file>`` as a release asset."""
        n = 0
        for f in sorted(Path(root).glob("*/*/releases/download/*/*")):
            if f.is_file():
                owner, repo, _r, _d, tag, name = f.relative_to(root).parts
                self.add(f"{owner}/{repo}", tag, name, f.read_bytes())
                n += 1
        return n

    def replace_bytes(self, name: str, data: bytes) -> None:
        for a in self._assets.values():
            if a["name"] == name:
                a["data"] = data

    # -- API views --

    @property
    def api(self) -> str:
        assert self._srv is not None
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def asset(self, aid: int) -> dict[str, Any] | None:
        return self._assets.get(aid)

    def asset_json(self, a: dict[str, Any]) -> dict[str, Any]:
        url = (f"https://evil.example.invalid/assets/{a['id']}" if self.bogus_asset_url
               else f"{self.api}/repos/{a['repo']}/releases/assets/{a['id']}")
        return {"id": a["id"], "name": a["name"], "size": len(a["data"]), "url": url,
                "browser_download_url": f"https://github.com/{a['repo']}/releases/download/"
                                        f"{a['tag']}/{a['name']}"}

    def assets_of(self, owner_repo: str, rid: int) -> list[dict[str, Any]]:
        for (repo, _tag), rel in self._releases.items():
            if repo == owner_repo and rel["id"] == rid:
                return [self._assets[i] for i in rel["assets"]]
        return []

    def release(self, owner_repo: str, tag: str) -> dict[str, Any] | None:
        rel = self._releases.get((owner_repo, tag))
        if rel is None:
            return None
        assets = [self._assets[i] for i in rel["assets"]]
        return {"id": rel["id"], "tag_name": tag,
                "assets_url": f"{self.api}/repos/{owner_repo}/releases/{rel['id']}/assets",
                "assets": [self.asset_json(a) for a in assets[:self.embed]]}

    def api_paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    # -- lifecycle --

    def __enter__(self) -> FakeGitHub:
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
