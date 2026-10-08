"""OTA-C: the github-release source (david U1: private GitHub Releases, with a token),
against a local fake of GitHub's REST API (``tests/fakes/fake_github.py``, 127.0.0.1 only).

- the rolling ``channel-<catalog>-<channel>`` release holds the signed channel; assets in
  per-version releases are looked up by tag through the API and downloaded from the
  asset's API URL; the token goes to the API, never to the storage redirect;
- twins: no token (a private repo answers 404), a wrong token (401), an asset URL the
  API reply points outside the API (refused, the token never sent there);
- a release with more assets than the release JSON embeds (paged ``assets_url``);
- the round trip with the release tool (OTA-R, ``tools/release``): its dry-run tree,
  served as GitHub releases, is fetched, verified and staged, pyverify pinned to the
  verified dep; twin: a tampered dep refuses the stage before anything is built.
"""

from __future__ import annotations

import json
import os

import pytest

from harness_manager.core.errors import RefusedError, UnavailableError, UnreachableError
from harness_manager.services.update import minisign, trust
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.appstage import stage_app
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.policy import Policy
from harness_manager.services.update.service import UpdateService
from harness_manager.services.update.state import UpdateState
from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore
from tests.fakes.fake_channel import TestKeys, sha256_bytes
from tests.fakes.fake_github import FakeGitHub
from tests.fakes.otar_release import FakeTools, make_repo
from tests.fakes.t7_board import FakeUv

KEYS = TestKeys()
REPO = "SoC-Labs/HarnessManager"
SRC = "github:" + REPO
WHEEL = b"PK-fake-wheel-0.2.0"
DEP = b"PK-fake-pyverify-0.1.0"


def app_doc(*, serial: int = 1, wheel_url: str = "../v0.2.0/harness_manager-0.2.0-py3-none-any.whl",
            dep_url: str = "../v0.2.0/mps3_pyverify-0.1.0-py3-none-any.whl") -> dict:
    return {"schema": "harness-manager-channel", "schema_version": 1, "catalog": "hm-app",
            "channel": "stable", "serial": serial, "issued_at": "2026-09-24T12:00:00Z",
            "signing_key_id": KEYS.release.public.id_hex,
            "app": {"current": "0.2.0", "releases": [{
                "version": "0.2.0", "status": "current", "notes": "Self-update.",
                "artifacts": [
                    {"kind": "wheel", "name": "harness_manager-0.2.0-py3-none-any.whl",
                     "url": wheel_url, "sha256": sha256_bytes(WHEEL), "size": len(WHEEL),
                     "access": "github-token", "repo": REPO},
                    {"kind": "dep", "name": "mps3_pyverify-0.1.0-py3-none-any.whl",
                     "url": dep_url, "sha256": sha256_bytes(DEP), "size": len(DEP),
                     "access": "github-token", "repo": REPO}]}]}}


def publish(gh: FakeGitHub, doc: dict, tag: str = "channel-hm-app-stable") -> None:
    data = json.dumps(doc).encode()
    gh.add(REPO, tag, "channel.json", data)
    gh.add(REPO, tag, "channel.json.minisig", minisign.sign(data, KEYS.release).encode())
    gh.add(REPO, "v0.2.0", "harness_manager-0.2.0-py3-none-any.whl", WHEEL)
    gh.add(REPO, "v0.2.0", "mps3_pyverify-0.1.0-py3-none-any.whl", DEP)


def client(tmp_path, gh: FakeGitHub, **kw) -> ChannelClient:
    state = UpdateState(tmp_path / "state" / "update")
    kw.setdefault("token", gh.token)
    dl = Downloader(state.cache, github_api=gh.api, mirrors=(), **kw)
    return ChannelClient(state, dl, KEYS.trust())


@pytest.fixture
def gh():
    with FakeGitHub() as fake:
        yield fake


def test_a_github_release_channel_and_its_assets_come_through_the_api(gh, tmp_path):
    publish(gh, app_doc())
    c = client(tmp_path, gh)
    v = c.fetch("stable", SRC, catalog="hm-app")
    assert v.url == (f"https://github.com/{REPO}/releases/download/channel-hm-app-stable/"
                     "channel.json")
    assert v.catalog == "hm-app" and v.channel.app_release().notes == "Self-update."
    rel = v.channel.app_release()
    assert c.downloader.fetch(rel.wheel, base_url=v.url).read_bytes() == WHEEL
    assert c.downloader.fetch(rel.deps[0], base_url=v.url).read_bytes() == DEP
    api = [r for r in gh.requests if r["path"].startswith("/repos/")]
    assert api and all(r["auth_ok"] for r in api)             # the token went to the API ...
    assert gh.storage_hits and not any(h["auth"] for h in gh.storage_hits)   # ... never to storage
    tags = [r["path"] for r in api if "/releases/tags/" in r["path"]]
    assert tags == [f"/repos/{REPO}/releases/tags/channel-hm-app-stable",
                    f"/repos/{REPO}/releases/tags/v0.2.0"]  # one lookup per release
    downloads = [r for r in api if "/releases/assets/" in r["path"]]
    assert all("application/octet-stream" in r["accept"] for r in downloads)


def test_twin_without_a_token_a_private_repo_is_not_found(gh, tmp_path):
    publish(gh, app_doc())
    with pytest.raises(UnreachableError, match="not found") as exc:
        client(tmp_path, gh, token=None).fetch("stable", SRC, catalog="hm-app")
    assert "HARNESS_MANAGER_GITHUB_TOKEN" in exc.value.hint
    assert not any(r["auth"] for r in gh.requests)


def test_twin_a_wrong_token_is_refused_without_echoing_it(gh, tmp_path):
    publish(gh, app_doc())
    wrong = "ghp_wrong_token_never_shown"
    with pytest.raises(UnavailableError, match="refused the token") as exc:
        client(tmp_path, gh, token=wrong).fetch("stable", SRC, catalog="hm-app")
    assert wrong not in str(exc.value) and wrong not in exc.value.hint


def test_twin_the_wrong_catalogue_tag_is_simply_absent(gh, tmp_path):
    publish(gh, app_doc())
    with pytest.raises(UnreachableError, match="not found"):
        client(tmp_path, gh).fetch("stable", SRC, catalog="mps3-harness")


def test_absolute_download_and_api_asset_urls_both_work(gh, tmp_path):
    publish(gh, app_doc())
    dep_id = next(i for i, a in gh._assets.items() if a["name"].startswith("mps3"))
    publish(gh, app_doc(serial=2,
                        wheel_url=f"https://github.com/{REPO}/releases/download/v0.2.0/"
                                  "harness_manager-0.2.0-py3-none-any.whl",
                        dep_url=f"{gh.api}/repos/{REPO}/releases/assets/{dep_id}"))
    c = client(tmp_path, gh)
    v = c.fetch("stable", SRC, catalog="hm-app")
    rel = v.channel.app_release()
    assert c.downloader.fetch(rel.wheel, base_url=v.url).read_bytes() == WHEEL
    assert c.downloader.fetch(rel.deps[0], base_url=v.url).read_bytes() == DEP
    dep_get = [r for r in gh.requests if r["path"].endswith(f"/releases/assets/{dep_id}")]
    assert dep_get and all(r["auth_ok"] for r in dep_get)     # the API URL carries the token


def test_an_asset_url_outside_the_api_is_refused_and_gets_no_token(gh, tmp_path):
    publish(gh, app_doc())
    gh.bogus_asset_url = True
    with pytest.raises(RefusedError, match="outside its API"):
        client(tmp_path, gh).fetch("stable", SRC, catalog="hm-app")
    assert all(r["path"].startswith("/repos/") for r in gh.requests)   # nothing else was asked


def test_a_release_with_more_assets_than_the_json_embeds_is_paged(tmp_path):
    with FakeGitHub(embed=30) as gh:
        publish(gh, app_doc())
        for i in range(34):
            gh.add(REPO, "v0.2.0", f"filler-{i:02d}.txt", b"x")   # the wheel is now past #30
        gh.add(REPO, "v0.2.0", "harness_manager-0.2.0-py3-none-any.whl", WHEEL)
        c = client(tmp_path, gh)
        v = c.fetch("stable", SRC, catalog="hm-app")
        assert c.downloader.fetch(v.channel.app_release().wheel,
                                  base_url=v.url).read_bytes() == WHEEL
        assert any(r["path"].endswith("/assets") and "page=1" in r["query"]
                   for r in gh.requests)                     # found through assets_url


# --- the round trip with the release tool (OTA-R) ------------------------------------------


def _release_tree(tmp_path):
    from tools.release import signer as signer_mod
    from tools.release.cli import main

    sk, pk = signer_mod.keygen_throwaway(tmp_path / "keys", "beta")
    repo = make_repo(tmp_path / "hm")
    out = tmp_path / "dist"
    lines: list[str] = []
    rc = main(["app", "--out", str(out), "--repo-root", str(repo), "--signer", "python",
               "--secret-key", str(sk), "--lock-tool", "uv", "--uv", "fake-uv"],
              runner=FakeTools(), printer=lines.append)
    assert rc == 0, "\n".join(lines)
    return out, minisign.PublicKey.from_text(pk.read_text()), lines


def _service(tmp_path, gh: FakeGitHub, pub: minisign.PublicKey, uv: FakeUv) -> UpdateService:
    state_dir = tmp_path / "client"
    app = AppUpdater(AppLayout(state_dir / "update" / "app"), LocalBusyProbe(state_dir),
                     uv="/opt/uv", runner=uv, python_version="3.11", running_version="0.1.0",
                     windows=os.name == "nt")
    store = TrustStore(pinned=(TrustedKey(pub, ROLE_RELEASE, trust.CHANNELS, "otar"),))
    dl = Downloader(state_dir / "update" / "cache", token=gh.token, github_api=gh.api, mirrors=())
    return UpdateService(state_dir=state_dir, trust=store, app_version="0.1.0", app_updater=app,
                         downloader=dl, policy=Policy())


def test_the_release_tools_tree_round_trips_through_github_and_stages(gh, tmp_path):
    out, pub, lines = _release_tree(tmp_path)
    assert not any("emitted as access: public" in ln for ln in lines)   # U1: private wheel now
    assert gh.publish_tree(out) >= 5
    uv = FakeUv()
    svc = _service(tmp_path, gh, pub, uv)
    res = stage_app(svc, channel="beta", source=SRC, catalog="hm-app")
    assert res["staged"] and res["version"] == "0.2.0"
    rel = svc.fetch_channel("beta", SRC, catalog="hm-app").channel.app_release()
    assert rel.wheel.access == "github-token" and rel.deps and rel.lock
    reqs = uv.reqs_seen[0]
    dep_line = next(ln for ln in reqs.splitlines() if ln.startswith("mps3-pyverify"))
    assert dep_line.startswith("mps3-pyverify @ file://")
    assert dep_line.endswith(f"mps3_pyverify-0.1.0-py3-none-any.whl --hash=sha256:"
                             f"{rel.deps[0].sha256}")
    assert "mps3-pyverify==" not in reqs                       # the resolver's line is replaced
    assert svc.app().state()["current"] == ""                  # staged beside, never switched
    assert not any(h["auth"] for h in gh.storage_hits)


def test_twin_a_tampered_dep_on_github_refuses_the_stage(gh, tmp_path):
    out, pub, _ = _release_tree(tmp_path)
    gh.publish_tree(out)
    name = "mps3_pyverify-0.1.0-py3-none-any.whl"
    good = next(a["data"] for a in gh._assets.values() if a["name"] == name)
    gh.replace_bytes(name, good[:-1] + bytes([good[-1] ^ 0xFF]))        # same size, other bytes
    uv = FakeUv()
    svc = _service(tmp_path, gh, pub, uv)
    with pytest.raises(RefusedError, match="fails its sha256 check"):
        stage_app(svc, channel="beta", source=SRC, catalog="hm-app")
    assert uv.calls == []                                       # nothing was built


# --- GUIDE-BUGS: `update check|app --channel beta` read the hm-app catalogue --------------------

def _publish_beta(gh):
    app = app_doc()
    app["channel"] = "beta"
    publish(gh, app, tag="channel-hm-app-beta")
    harness = app_doc()          # a stated catalogue is enough to tell the two apart
    harness.update(catalog="mps3-harness", channel="beta")
    data = json.dumps(harness).encode()
    gh.add(REPO, "channel-mps3-harness-beta", "channel.json", data)
    gh.add(REPO, "channel-mps3-harness-beta", "channel.json.minisig",
           minisign.sign(data, KEYS.release).encode())


def test_the_app_update_commands_ask_for_the_hm_app_catalogue(monkeypatch):
    """The 404 of 1.0.2: the CLI asked for no catalogue, so the tag was `channel-beta`;
    the published release is `channel-hm-app-beta`."""
    from harness_manager.cli import cmd_update

    asked = []

    class Svc:
        def check(self, **kw):
            asked.append(("check", kw.get("catalog")))
            raise SystemExit(0)

        def fetch_channel(self, channel=None, source=None, *, catalog=None):
            asked.append(("app", catalog))
            raise SystemExit(0)

    monkeypatch.setattr(cmd_update, "service", lambda ctx: Svc())

    class Ctx:
        class args:                                              # noqa: N801
            target, channel, source = None, "beta", SRC
            apply = stage_only = False
            want_version = None

    for fn in (cmd_update._check, cmd_update._app):
        with pytest.raises(SystemExit):
            fn(Ctx())
    assert asked == [("check", "hm-app"), ("app", "hm-app")]


def test_beta_resolves_for_the_app_catalogue_and_the_harness_catalogue(gh, tmp_path):
    _publish_beta(gh)
    c = client(tmp_path, gh)
    app = c.fetch("beta", SRC, catalog="hm-app")
    assert app.url.endswith("/channel-hm-app-beta/channel.json") and app.catalog == "hm-app"
    harness = c.fetch("beta", SRC, catalog="mps3-harness")      # twin: still resolves
    assert harness.url.endswith("/channel-mps3-harness-beta/channel.json")
    # twin: with no catalogue the tag is `channel-beta`, which nobody publishes
    with pytest.raises(UnreachableError, match="channel-beta"):
        client(tmp_path / "x", gh).fetch("beta", SRC)


@pytest.mark.skipif(not os.environ.get("HM_LIVE_PUBLIC"), reason="reads the real public repo")
def test_live_the_public_app_channel_beta_resolves():
    """Read-only against SoC-Labs/HarnessManager (public, no token). Set HM_LIVE_PUBLIC=1."""
    import urllib.request

    from harness_manager.services.update.channel import channel_url

    url = channel_url("github:SoC-Labs/HarnessManager", "beta", "hm-app")
    assert url.endswith("/channel-hm-app-beta/channel.json")
    assert urllib.request.urlopen(url, timeout=30).status == 200
