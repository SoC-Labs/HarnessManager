"""OTA-R: the lead-run release tool, app side (tools/release). Every test has a negative twin.

- a dry-run release verifies with HM's own channel client (UpdateService.check offers it,
  the Downloader fetches the wheel); twin: a client pinning another key refuses it;
- a tampered artifact is refused (the tool's smoke AND HM's downloader); a tampered
  channel is refused by the signature check;
- a dirty tree is refused; twin: --allow-dirty dry-runs, and never publishes;
- promote beta -> stable re-signs (another key, serial 1, same entry, beta untouched);
  twins: a version not on beta, and promoting twice;
- the version must be bumped everywhere and the tag free; withdraw keeps the release;
- the key source: minisign CLI (a stand-in) or HM's signer; encrypted keys and keys outside
  the temp dir are refused; publishing needs the minisign CLI and a pinned key;
- --publish runs the gh plan in order (a recording runner: nothing leaves the machine);
- --mirror writes the GitHub layout plus blobs/<sha256>, and the client reads it.

No network, no real key, no gh: the wheel, the lock and gh are fakes (tests/fakes/otar_release.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services.update import minisign, trust
from harness_manager.services.update.service import UpdateService
from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore
from tests.fakes.otar_release import FakeTools, commit_all, fake_minisign, make_repo, tag
from tools.release import signer as signer_mod
from tools.release import smoke
from tools.release.cli import main
from tools.release.common import Layout, ReleaseError, sha256_file

APP = "hm-app"


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, Path]:
    sk, pk = signer_mod.keygen_throwaway(tmp_path / "keys", "beta")
    sk2, pk2 = signer_mod.keygen_throwaway(tmp_path / "keys", "stable")
    return {"sk": sk, "pk": pk, "sk2": sk2, "pk2": pk2}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "hm")


def _pub(path: Path) -> minisign.PublicKey:
    return minisign.PublicKey.from_text(path.read_text())


def app_args(out: Path, repo: Path, sk: Path, *extra: str) -> list[str]:
    return ["app", "--out", str(out), "--repo-root", str(repo), "--signer", "python",
            "--secret-key", str(sk), "--lock-tool", "uv", "--uv", "fake-uv", *extra]


def run_main(argv: list[str], tools: FakeTools | None = None) -> tuple[int, list[str]]:
    lines: list[str] = []
    rc = main(argv, runner=tools or FakeTools(), printer=lines.append)
    return rc, lines


def client(tmp_path: Path, *pubs: Path, name: str = "client") -> UpdateService:
    store = TrustStore(pinned=tuple(TrustedKey(_pub(p), ROLE_RELEASE, trust.CHANNELS, "t")
                                    for p in pubs))
    return UpdateService(state_dir=tmp_path / name, trust=store, token="", app_version="0.1.0")


# --- a dry run, read back by HM's own client -------------------------------------------------


def test_dry_run_release_verifies_with_hms_own_client(tmp_path, repo, keys):
    out, tools = tmp_path / "dist", FakeTools()
    rc, lines = run_main(app_args(out, repo, keys["sk"]), tools)
    assert rc == 0, "\n".join(lines)
    layout = Layout(out)
    ch = layout.channel_file(APP, "beta")
    assert ch.is_file() and ch.with_name("channel.json.minisig").is_file()
    doc = json.loads(ch.read_bytes())
    assert doc["catalog"] == APP and doc["serial"] == 1 and doc["channel"] == "beta"
    rel = doc["app"]["releases"][0]
    kinds = [a["kind"] for a in rel["artifacts"]]
    assert kinds == ["wheel", "dep"] and rel["lock"]["name"] == "harness_manager-0.2.0.lock.txt"
    assert rel["notes"].startswith("- Self-update") and rel["lock_info"]["universal"] is True
    lock = (layout.asset_path("v0.2.0", rel["lock"]["name"])).read_text()
    dep = layout.asset_path("v0.2.0", "mps3_pyverify-0.1.0-py3-none-any.whl")
    assert f"mps3-pyverify==0.1.0 --hash=sha256:{sha256_file(dep)}" in lock
    assert "c" * 64 not in lock                      # the resolver's hash was replaced
    # HM's own client: signature, schema, serial; the app update is offered
    svc = client(tmp_path, keys["pk"])
    report = svc.check(channel="beta", source=str(ch))
    assert report["app_update"] == "0.2.0" and report["serial"] == 1
    verified = svc.fetch_channel("beta", str(ch))
    wheel = svc.downloader.fetch(verified.channel.app_release().wheel, base_url=verified.url)
    assert sha256_file(wheel) == rel["artifacts"][0]["sha256"]
    # a dry run runs no gh; the plan is written
    assert tools.gh_calls() == []
    plan = (out / "plans" / f"{APP}-beta" / "publish-plan.txt").read_text()
    assert "gh release create v0.2.0" in plan and "channel-hm-app-beta" in plan
    assert any("DRY RUN" in ln for ln in lines)


def test_twin_a_client_pinning_another_key_refuses_the_release(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    svc = client(tmp_path, keys["pk2"])
    with pytest.raises(RefusedError, match="does not trust"):
        svc.check(channel="beta", source=str(Layout(out).channel_file(APP, "beta")))


# --- tampering ---------------------------------------------------------------------------------


def test_a_tampered_artifact_is_refused(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    layout = Layout(out)
    rep = smoke.verify(layout, APP, "beta", [_pub(keys["pk"])])        # twin: untouched passes
    assert rep.version == "0.2.0"
    wheel = layout.asset_path("v0.2.0", "harness_manager-0.2.0-py3-none-any.whl")
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    wheel.write_bytes(bytes(data))
    with pytest.raises(ReleaseError, match="sha256"):
        smoke.verify(layout, APP, "beta", [_pub(keys["pk"])])
    svc = client(tmp_path, keys["pk"])
    verified = svc.fetch_channel("beta", str(layout.channel_file(APP, "beta")))
    with pytest.raises(RefusedError, match="sha256"):
        svc.downloader.fetch(verified.channel.app_release().wheel, base_url=verified.url)


def test_a_tampered_channel_is_refused(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    ch = Layout(out).channel_file(APP, "beta")
    ch.write_bytes(ch.read_bytes().replace(b'"serial": 1', b'"serial": 9'))
    with pytest.raises(ReleaseError, match="refuses"):
        smoke.verify(Layout(out), APP, "beta", [_pub(keys["pk"])])


# --- preconditions -------------------------------------------------------------------------------


def test_a_dirty_tree_is_refused(tmp_path, repo, keys):
    (repo / "src" / "harness_manager" / "stray.py").write_text("x = 1\n")
    out = tmp_path / "dist"
    rc, lines = run_main(app_args(out, repo, keys["sk"]))
    assert rc == 15 and "not clean" in "\n".join(lines)
    assert not out.exists()


def test_twin_allow_dirty_dry_runs_but_never_publishes(tmp_path, repo, keys):
    (repo / "stray.txt").write_text("x\n")
    out = tmp_path / "dist"
    rc, lines = run_main(app_args(out, repo, keys["sk"], "--allow-dirty"))
    assert rc == 0 and any("DIRTY TREE" in ln for ln in lines)
    rc, lines = run_main(app_args(tmp_path / "d2", repo, keys["sk"], "--allow-dirty",
                                  "--publish"))
    assert rc == 15


def test_the_version_must_be_bumped_everywhere(tmp_path, keys):
    repo = make_repo(tmp_path / "hm", "0.2.0", init_version="0.1.9")
    rc, lines = run_main(app_args(tmp_path / "dist", repo, keys["sk"]))
    assert rc == 15 and "__version__ says 0.1.9" in "\n".join(lines)


def test_a_tag_elsewhere_is_refused_and_a_tag_at_head_is_fine(tmp_path, repo, keys):
    tag(repo, "v0.2.0")
    (repo / "README").write_text("later\n")
    commit_all(repo, "later")
    rc, lines = run_main(app_args(tmp_path / "dist", repo, keys["sk"]))
    assert rc == 15 and "already exists" in "\n".join(lines)
    repo2 = make_repo(tmp_path / "hm2")
    tag(repo2, "v0.2.0")
    assert run_main(app_args(tmp_path / "d2", repo2, keys["sk"]))[0] == 0


def test_a_released_version_is_never_rewritten(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    rc, lines = run_main(app_args(out, repo, keys["sk"]))
    assert rc == 15 and "already on the 'beta' channel" in "\n".join(lines)
    older = make_repo(tmp_path / "old", "0.1.5")
    rc, lines = run_main(app_args(out, older, keys["sk"]))
    assert rc == 15 and "not newer than 0.2.0" in "\n".join(lines)


def test_stable_takes_a_release_only_by_promote(tmp_path, repo, keys):
    rc, lines = run_main(app_args(tmp_path / "dist", repo, keys["sk"], "--channel", "stable"))
    assert rc == 2 and "promote" in "\n".join(lines)


# --- promote + withdraw ------------------------------------------------------------------------


def test_promote_beta_to_stable_re_signs(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    layout = Layout(out)
    beta = layout.channel_file(APP, "beta")
    beta_bytes = beta.read_bytes()
    rc, lines = run_main(["promote", "--out", str(out), "--version", "0.2.0", "--signer",
                          "python", "--secret-key", str(keys["sk2"]),
                          "--trust-key", str(keys["pk"])])
    assert rc == 0, "\n".join(lines)
    stable = layout.channel_file(APP, "stable")
    sig = minisign.parse_signature(stable.with_name("channel.json.minisig").read_bytes())
    assert sig.key_id_hex == _pub(keys["pk2"]).id_hex             # re-signed, by the stable key
    minisign.verify(stable.read_bytes(), sig, _pub(keys["pk2"]))
    s_doc, b_doc = json.loads(stable.read_bytes()), json.loads(beta_bytes)
    assert s_doc["serial"] == 1 and s_doc["app"]["current"] == "0.2.0"
    assert s_doc["app"]["releases"][0]["artifacts"] == b_doc["app"]["releases"][0]["artifacts"]
    assert beta.read_bytes() == beta_bytes                          # beta untouched
    report = client(tmp_path, keys["pk2"]).check(channel="stable", source=str(stable))
    assert report["app_update"] == "0.2.0"
    plan = (out / "plans" / f"{APP}-stable" / "publish-plan.txt").read_text()
    assert "gh release upload channel-hm-app-stable" in plan and "release create v0.2.0" not in plan


def test_twin_promote_refuses_a_missing_version_and_a_second_promote(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    base = ["promote", "--out", str(out), "--signer", "python", "--secret-key", str(keys["sk"])]
    rc, lines = run_main([*base, "--version", "0.9.0"])
    assert rc == 15 and "not on the 'beta' channel" in "\n".join(lines)
    assert run_main([*base, "--version", "0.2.0"])[0] == 0
    rc, lines = run_main([*base, "--version", "0.2.0"])
    assert rc == 15 and "already current" in "\n".join(lines)


def test_withdraw_keeps_the_release_listed(tmp_path, repo, keys):
    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    base = ["withdraw", "--out", str(out), "--channel", "beta", "--signer", "python",
            "--secret-key", str(keys["sk"])]
    assert run_main([*base, "--version", "0.2.0", "--reason", "breaks the console"])[0] == 0
    doc = json.loads(Layout(out).channel_file(APP, "beta").read_bytes())
    rel = doc["app"]["releases"][0]
    assert doc["serial"] == 2 and rel["status"] == "withdrawn" and "current" not in doc["app"]
    assert rel["withdrawn_reason"] == "breaks the console"
    rc, lines = run_main([*base, "--version", "0.2.0", "--reason", "again"])
    assert rc == 15 and "already withdrawn" in "\n".join(lines)


def test_each_catalogue_keeps_its_own_channel_and_serial(tmp_path, repo, keys):
    from tools.release.channel_doc import ChannelDoc

    out = tmp_path / "dist"
    assert run_main(app_args(out, repo, keys["sk"]))[0] == 0
    ch = Layout(out).channel_file(APP, "beta")
    assert "channel-hm-app-beta" in str(ch)                  # one rolling release per pair
    assert not Layout(out).channel_file("mps3-harness", "beta").exists()
    with pytest.raises(ReleaseError, match="catalogue 'hm-app'"):
        ChannelDoc.load(ch, "mps3-harness", "beta", [_pub(keys["pk"])])
    assert ChannelDoc.load(ch, APP, "beta", [_pub(keys["pk"])]).base_serial == 1


# --- the key source --------------------------------------------------------------------------


def test_throwaway_key_round_trip_and_refusals(tmp_path):
    sk, pk = signer_mod.keygen_throwaway(tmp_path / "k")
    assert oct(sk.stat().st_mode & 0o777) == "0o600"
    s = signer_mod.PythonSigner(sk)
    assert s.public.id_hex == _pub(pk).id_hex
    f = tmp_path / "f.txt"
    f.write_text("hello")
    minisign.verify(f.read_bytes(), s.sign(f, "t"), _pub(pk))
    # an encrypted (scrypt) key is never decrypted in Python
    import base64

    blob = bytearray(base64.b64decode(sk.read_text().splitlines()[1]))
    blob[2:4] = b"Sc"
    enc = tmp_path / "enc.key"
    enc.write_text("untrusted comment: x\n" + base64.b64encode(bytes(blob)).decode() + "\n")
    with pytest.raises(ReleaseError, match="passphrase-protected"):
        signer_mod.PythonSigner(enc)
    with pytest.raises(ReleaseError, match="temp dir only"):
        signer_mod.keygen_throwaway(Path.home() / "otar-should-not-exist")
    assert not (Path.home() / "otar-should-not-exist").exists()


def test_the_minisign_cli_signs_and_is_checked(tmp_path, repo, keys):
    fake = fake_minisign(tmp_path)
    s = signer_mod.from_options("minisign", str(keys["sk"]), str(keys["pk"]), str(fake))
    f = tmp_path / "channel.json"
    f.write_text("{}")
    minisign.verify(f.read_bytes(), s.sign(f, "catalog:hm-app"), _pub(keys["pk"]))
    argv = (tmp_path / "channel.json.minisig.argv").read_text()
    assert argv.startswith("-S -s ") and "-t catalog:hm-app" in argv
    wrong = signer_mod.from_options("minisign", str(keys["sk"]), str(keys["pk2"]), str(fake))
    with pytest.raises(ReleaseError, match="not a pair"):
        wrong.sign(f, "x")
    # and from the environment
    env = {"HM_RELEASE_SIGNER": "minisign", "HM_RELEASE_SECRET_KEY": str(keys["sk"]),
           "HM_RELEASE_PUBLIC_KEY": str(keys["pk"]), "HM_MINISIGN": str(fake)}
    assert signer_mod.from_options(None, None, None, env=env).public.id_hex == \
        _pub(keys["pk"]).id_hex
    with pytest.raises(ReleaseError, match="no signing key"):
        signer_mod.from_options(None, None, None, env={})
    # a whole release signed through the CLI verifies too
    out = tmp_path / "dist"
    rc, lines = run_main(["app", "--out", str(out), "--repo-root", str(repo), "--secret-key",
                          str(keys["sk"]), "--public-key", str(keys["pk"]), "--minisign",
                          str(fake), "--lock-tool", "uv", "--uv", "fake-uv"])
    assert rc == 0, "\n".join(lines)
    assert any("minisign key" in ln for ln in lines)


# --- publishing ---------------------------------------------------------------------------------


def _publishable(repo: Path) -> None:
    tag(repo, "v0.2.0")


def test_publish_refuses_the_python_signer_and_an_unpinned_key(tmp_path, repo, keys):
    _publishable(repo)
    tools = FakeTools()
    rc, lines = run_main(app_args(tmp_path / "d", repo, keys["sk"], "--publish"), tools)
    assert rc == 15 and "not pinned" in "\n".join(lines)
    rc, lines = run_main(app_args(tmp_path / "d", repo, keys["sk"], "--publish",
                                  "--allow-unpinned-key"), tools)
    assert rc == 15 and "minisign CLI only" in "\n".join(lines)
    assert tools.gh_calls() == []


def test_publish_runs_the_gh_plan_in_order(tmp_path, repo, keys, monkeypatch):
    _publishable(repo)
    monkeypatch.setattr(trust, "PINNED_KEYS", (TrustedKey(_pub(keys["pk"]), ROLE_RELEASE,
                                                          trust.CHANNELS, "test"),))
    fake = fake_minisign(tmp_path)
    tools = FakeTools()
    rc, lines = run_main(["app", "--out", str(tmp_path / "d"), "--repo-root", str(repo),
                          "--secret-key", str(keys["sk"]), "--public-key", str(keys["pk"]),
                          "--minisign", str(fake), "--lock-tool", "uv", "--uv", "fake-uv",
                          "--access", "public", "--gh", "fake-gh", "--publish",
                          "--skip-install-smoke", "test"], tools)
    assert rc == 0, "\n".join(lines)
    gh = [" ".join(c[1:4]) for c in tools.gh_calls()]
    assert gh[0] == "release view channel-hm-app-beta"               # the live base (none yet)
    assert gh[1] == "release create v0.2.0" and gh[-1] == "release upload channel-hm-app-beta"
    push = [c for c in tools.calls if c[:1] == ["git"] and "push" in c]
    assert push and push[0][1:3] == ["-C", str(repo)]               # the release checkout's tag
    assert tools.calls.index(push[0]) < tools.calls.index(tools.gh_calls()[1])


def test_twin_publish_refuses_a_non_universal_lock(tmp_path, repo, keys, monkeypatch):
    _publishable(repo)
    monkeypatch.setattr(trust, "PINNED_KEYS", (TrustedKey(_pub(keys["pk"]), ROLE_RELEASE,
                                                          trust.CHANNELS, "test"),))
    tools = FakeTools()
    rc, lines = run_main(["app", "--out", str(tmp_path / "d"), "--repo-root", str(repo),
                          "--secret-key", str(keys["sk"]), "--public-key", str(keys["pk"]),
                          "--minisign", str(fake_minisign(tmp_path)), "--lock-tool",
                          "pip-tools", "--pip-compile", "fake-pip-compile", "--access",
                          "public", "--gh", "fake-gh", "--publish", "--skip-install-smoke", "t"],
                         tools)
    assert rc == 15 and "not universal" in "\n".join(lines)
    assert [c for c in tools.gh_calls() if c[1:3] != ["release", "view"]] == []


# --- the mirror ----------------------------------------------------------------------------------


def test_mirror_layout_is_readable_by_the_client(tmp_path, repo, keys):
    out, mirror_dir = tmp_path / "dist", tmp_path / "hub-mirror"
    assert run_main(app_args(out, repo, keys["sk"], "--mirror", str(mirror_dir)))[0] == 0
    doc = json.loads(Layout(out).channel_file(APP, "beta").read_bytes())
    wheel_sha = doc["app"]["releases"][0]["artifacts"][0]["sha256"]
    assert (mirror_dir / "blobs" / wheel_sha).is_file()
    mch = Layout(mirror_dir).channel_file(APP, "beta")
    svc = client(tmp_path, keys["pk"])
    verified = svc.fetch_channel("beta", str(mch))
    wheel = svc.downloader.fetch(verified.channel.app_release().wheel, base_url=verified.url)
    assert sha256_file(wheel) == wheel_sha
