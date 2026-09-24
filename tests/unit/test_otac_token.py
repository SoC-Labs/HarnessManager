"""OTA-C (P9): the GitHub token goes on the channel index fetch, to token hosts only, and is
never logged or echoed. Every check has a negative twin. Only 127.0.0.1 is contacted.
"""

from __future__ import annotations

import json
import logging
import subprocess

import pytest

from harness_manager.core.errors import HarnessError, UnavailableError, UnreachableError
from harness_manager.services.update import github
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.service import UpdateService
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_bundles import Release

KEYS = TestKeys()
LOOP = frozenset({"127.0.0.1"})


@pytest.fixture
def server(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        b = ChannelBuilder(srv.root / "private", KEYS)       # the fake serves private/ with a token
        Release("1.1.0").add_to(b, tmp_path / "art")
        b.publish(serial=3)
        yield srv


def client(tmp_path, **kw) -> ChannelClient:
    state = UpdateState(tmp_path / "state" / "update")
    return ChannelClient(state, Downloader(state.cache, mirrors=(), **kw), KEYS.trust())


def private_source(srv) -> str:
    return srv.base + "private/channel/{channel}/channel.json"


def test_the_index_fetch_carries_the_token_to_a_token_host(server, tmp_path):
    v = client(tmp_path, token=server.token, token_hosts=LOOP).fetch("stable",
                                                                     private_source(server))
    assert v.channel.serial == 3                              # P9: a private index is readable
    idx = [r for r in server.requests if r["path"].endswith((".json", ".minisig"))]
    assert len(idx) == 2 and all(r["auth_value_ok"] for r in idx)


def test_twin_the_token_never_goes_to_a_host_that_is_not_a_token_host(server, tmp_path):
    with pytest.raises(HarnessError):                         # default hosts: GitHub only
        client(tmp_path, token=server.token).fetch("stable", private_source(server))
    assert server.requests and not any(r["auth"] for r in server.requests)


def test_twin_a_private_index_without_a_token_says_how_to_get_one(server, tmp_path):
    with pytest.raises(UnavailableError, match="private.*HARNESS_MANAGER_GITHUB_TOKEN"):
        client(tmp_path, token_hosts=LOOP).fetch("stable", private_source(server))


def test_the_token_is_never_logged_nor_echoed(server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    good = client(tmp_path, token=server.token, token_hosts=LOOP)
    good.fetch("stable", private_source(server))
    texts = [caplog.text, repr(good.downloader), json.dumps(good.downloader.stats.requests)]
    bad_token = "ghp_wrong_token_value_must_not_leak"
    for dl_kw in ({"token": bad_token, "token_hosts": LOOP},        # a wrong token: refused
                  {"token": server.token, "token_hosts": LOOP}):    # the right one, 404
        c = client(tmp_path / "x", **dl_kw)
        src = private_source(server) if dl_kw["token"] == bad_token else \
            server.base + "private/channel/{channel}/missing.json"
        with pytest.raises(HarnessError) as exc:
            c.fetch("stable", src)
        texts += [str(exc.value), exc.value.hint, repr(c.downloader)]
    joined = "\n".join(texts)
    for tok in (server.token, bad_token):
        assert tok not in joined
    assert "refused the token" in joined                     # the wrong token was reported ...
    assert "(HTTP 401)" in joined                             # ... by its status, not its value


def test_the_token_provider_runs_only_when_a_token_host_is_asked(server, tmp_path):
    calls: list[int] = []

    def provider() -> str:
        calls.append(1)
        return server.token

    c = client(tmp_path, token_provider=provider)             # default hosts: not 127.0.0.1
    with pytest.raises(HarnessError):
        c.fetch("stable", private_source(server))
    assert calls == []                                        # never resolved for a non-GitHub host
    c2 = client(tmp_path / "2", token_provider=provider, token_hosts=LOOP)
    assert c2.fetch("stable", private_source(server)).channel.serial == 3
    assert calls == [1]                                       # twin: once, on first need


def test_gh_cli_token_reads_the_cli_and_logs_nothing_of_it(caplog):
    caplog.set_level(logging.DEBUG)
    secret = "gho_from_the_gh_cli_never_log_me"
    seen: list[list[str]] = []

    def ok(argv):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, secret + "\n", "")

    assert github.gh_cli_token(runner=ok, gh="/usr/bin/gh") == secret
    assert seen == [["/usr/bin/gh", "auth", "token", "--hostname", "github.com"]]

    def logged_out(argv):
        return subprocess.CompletedProcess(argv, 1, "", f"not logged in {secret}")

    def broken(argv):
        raise OSError("no such file")

    assert github.gh_cli_token(runner=logged_out, gh="/usr/bin/gh") is None     # twins
    assert github.gh_cli_token(runner=broken, gh="/usr/bin/gh") is None
    assert secret not in caplog.text


def test_resolve_token_prefers_the_environment_over_gh():
    def gh(argv):
        return subprocess.CompletedProcess(argv, 0, "from-gh\n", "")

    assert github.resolve_token({github.TOKEN_ENV: "from-env"}, runner=gh, gh="gh") == "from-env"
    assert github.resolve_token({}, runner=gh, gh="gh") == "from-gh"             # twin: fallback


def test_the_service_resolves_the_token_lazily_unless_one_is_given(tmp_path):
    svc = UpdateService(state_dir=tmp_path / "s", trust=KEYS.trust(), app_version="0.1.0")
    assert svc.downloader.token_provider is github.resolve_token
    assert "on demand" in repr(svc.downloader)
    given = UpdateService(state_dir=tmp_path / "t", trust=KEYS.trust(), app_version="0.1.0",
                          token="")
    assert given.downloader.token_provider is None and not given.downloader.has_token()


def test_an_unreachable_index_stays_unreachable(tmp_path):
    with pytest.raises(UnreachableError):
        client(tmp_path).fetch("stable", str(tmp_path / "nowhere"))
