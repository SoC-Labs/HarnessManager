"""SET-HUBS: hubs as named settings: resolution, machine hubs, the token, the board's
``hub.use``, discovery's board, "Make this a hub" and the hub's lease settings.

Every check has a negative twin: the same setup without the deciding part, which must give
a different answer (so the check bites). Nothing here reaches a network or a real hub:
host names are under ``.invalid``, and the REST hub is T8's fake on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    RefusedError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.settings import hubs as H
from harness_manager.settings.secrets import KeyringBackend, SecretStore
from harness_manager.transports import hub_rest
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import tunnel as T

HOST = "hub.invalid"
ADDR = "192.168.10.101"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No real policy, no fpgahub login, no token in the environment."""
    monkeypatch.setattr(H, "POLICY_PATH", tmp_path / "no-policy.toml")
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


def state() -> Path:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings(text: str) -> None:
    (state() / "settings.toml").write_text(text)


def boards(text: str) -> Path:
    path = state() / "boards.toml"
    path.write_text(text)
    return path


def policy(tmp_path, monkeypatch, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text)
    monkeypatch.setattr(H, "POLICY_PATH", path)
    return path


def cand(addr: str = ADDR) -> Candidate:
    return Candidate(pack="mps3", board_id=f"mps3@{addr}:6900",
                     links=(Link(LinkKind.ETHERNET, f"{addr}:6900", "shell control channel"),),
                     label="MPS3", evidence="test")


LAB = f'[hubs.lab]\nhost = "{HOST}"\njump = "bastion.invalid"\nholder = "me@desk"\n' \
      'lease_ttl = "15m"\nrequest_ttl = "30m"\nqueue_timeout = 600\n'
USE_LAB = f'[boards.lab]\nmatch = ["{ADDR}"]\nvia = "hub"\n' \
          'hub = { use = "lab", target = "mps3_01_pl", shares = { mcc = "/dev/mps3_01_pl/tty_00" } }\n'


# --- resolution ------------------------------------------------------------------------------


def test_an_ssh_hub_resolves_every_key_and_says_where():
    settings(LAB)
    hub = H.resolve_hub("lab", H.load_resolver())
    assert (hub.transport, hub.host, hub.group, hub.jump, hub.holder) == \
        ("ssh", HOST, "fpga", "bastion.invalid", "me@desk")
    assert (hub.lease_ttl, hub.request_ttl, hub.queue_timeout) == (900, 1800, 600)
    assert hub.table() == {"host": HOST, "group": "fpga"}
    assert hub.rows["host"].source == "user" and hub.rows["group"].source == "default"
    assert not hub.machine and hub.locked == [] and hub.config_problems() == []


def test_negative_twin_an_undeclared_hub_is_a_usage_error_naming_the_way_to_add_one():
    settings(LAB)
    with pytest.raises(UsageError) as ei:
        H.resolve_hub("elsewhere", H.load_resolver())
    assert "no hub named 'elsewhere'" in ei.value.message and "hub add" in ei.value.hint


def test_a_rest_hub_is_rest_because_it_has_a_url():
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\nca_file = "~/ca.pem"\n')
    hub = H.resolve_hub("remote", H.load_resolver())
    assert hub.transport == "rest" and hub.token_ref == "store"
    assert hub.table() == {"url": "https://hub.invalid:7246", "insecure": False, "events": True,
                           "direct": "auto", "timeout_s": 30, "ca_file": "~/ca.pem"}


def test_negative_twin_plain_http_off_box_is_a_config_problem_naming_the_key():
    settings('[hubs.remote]\nurl = "http://hub.invalid:7246"\n')
    probs = H.resolve_hub("remote", H.load_resolver()).config_problems()
    assert probs and "plain http" in probs[0] and "hubs.remote.url" in probs[0]


def test_no_hub_at_all_is_the_zero_config_default():
    r = H.load_resolver()
    assert H.list_hubs(r) == [] and H.inline_hub_boards(r) == []
    assert hubmod.hub_config_for(cand()) is None


# --- a board's hub.use (CCR SET-HUB-1) -------------------------------------------------------


def test_a_board_naming_an_ssh_hub_gets_its_keys_jump_holder_and_lease_times():
    settings(LAB)
    boards(USE_LAB)
    cfg = hubmod.hub_config_for(cand())
    assert (cfg.host, cfg.target, cfg.group, cfg.name, cfg.jump, cfg.holder) == \
        (HOST, "mps3_01_pl", "fpga", "lab", "bastion.invalid", "me@desk")
    assert (cfg.lease_ttl, cfg.request_ttl, cfg.queue_timeout) == (900, 1800, 600)
    assert cfg.shares == {"mcc": "/dev/mps3_01_pl/tty_00"} and cfg.rest is None


def test_negative_twin_an_inline_table_keeps_working_exactly_as_before():
    boards(f'[boards.lab]\nmatch = ["{ADDR}"]\n'
           f'hub = {{ host = "{HOST}", target = "mps3_01_pl" }}\n')
    cfg = hubmod.hub_config_for(cand())
    assert (cfg.host, cfg.name, cfg.jump, cfg.holder, cfg.lease_ttl) == (HOST, "", "", "", 0)


def test_a_board_naming_a_rest_hub_carries_the_hub_name_for_its_token(tmp_path):
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\nhost = "ssh.invalid"\n')
    boards(f'[boards.lab]\nmatch = ["{ADDR}"]\nhub = {{ use = "remote", target = "kr260_01_ps" }}\n')
    cfg = hubmod.hub_config_for(cand())
    assert cfg.transport == "rest" and cfg.rest.hub_name == "remote"
    assert cfg.rest.target == "kr260_01_ps" and cfg.rest.ssh_host == "ssh.invalid"
    assert cfg.rest.token_file == "" and cfg.rest.settings_root == str(state())


@pytest.mark.parametrize("key, value", [("host", '"other.invalid"'), ("url", '"https://x"'),
                                        ("token_file", '"~/t"'), ("group", '"g"')])
def test_use_beside_a_hub_key_is_refused_naming_the_key(key, value):
    settings(LAB)
    boards(f'[boards.lab]\nmatch = ["{ADDR}"]\nhub = {{ use = "lab", {key} = {value} }}\n')
    with pytest.raises(UsageError) as ei:
        hubmod.hub_config_for(cand())
    assert key in ei.value.message and "[hubs.lab]" in ei.value.message


def test_negative_twin_use_beside_only_board_keys_is_accepted():
    settings(LAB)
    boards(f'[boards.lab]\nmatch = ["{ADDR}"]\nhub = {{ use = "lab", target = "t1", baud = 9600, '
           'board = "b1", start_shares = true }\n')
    cfg = hubmod.hub_config_for(cand())
    assert (cfg.target, cfg.baud, cfg.board, cfg.start_shares) == ("t1", 9600, "b1", True)


def test_a_board_naming_a_hub_that_does_not_exist_gives_no_adapter_and_says_why():
    boards(USE_LAB)
    with pytest.raises(UsageError) as ei:
        hubmod.hub_config_for(cand())
    assert "no hub named 'lab'" in ei.value.message
    assert hubmod.adapter_for(cand()) is None              # the pack logs it, never crashes


# --- machine hubs (S3) -----------------------------------------------------------------------


MACHINE = f'[hubs.lab]\ntransport = "ssh"\nhost = "{HOST}"\ngroup = "fpga"\n'


def test_a_machine_hub_is_locked_but_the_users_token_stays_theirs(tmp_path, monkeypatch):
    pol = policy(tmp_path, monkeypatch, MACHINE.replace('transport = "ssh"', 'transport = "rest"'
                 ).replace(f'host = "{HOST}"', 'url = "https://hub.invalid:7246"'))
    r = H.load_resolver()
    hub = H.resolve_hub("lab", r)
    assert hub.machine and hub.policy == str(pol) and set(hub.locked) >= {"url", "transport"}
    with pytest.raises(RefusedError) as ei:
        H.add_hub("lab", {"url": "https://mine.invalid:7246"}, r, update=True)
    assert ei.value.code == ExitCode.REFUSED and "hub token lab" in ei.value.hint
    with pytest.raises(RefusedError):
        H.remove_hub("lab", r)
    status = H.set_hub_token("lab", r, value="users-own-token")
    assert status["set"] and status["backend"] == "file"
    assert (state() / "secrets").is_dir() and not (tmp_path / "policy.toml").read_text().count("token")


def test_negative_twin_a_users_hub_of_the_same_shape_can_be_changed_and_removed():
    settings(MACHINE)
    r = H.load_resolver()
    hub = H.add_hub("lab", {"host": "other.invalid"}, r, update=True)
    assert not hub.machine and hub.locked == [] and hub.host == "other.invalid"
    assert H.remove_hub("lab", r)["removed"] == "lab"
    assert "hubs" not in (state() / "settings.toml").read_text()


def test_a_machine_hubs_locked_key_beats_the_users_file(tmp_path, monkeypatch):
    policy(tmp_path, monkeypatch, MACHINE)
    settings('[hubs.lab]\nhost = "mine.invalid"\nholder = "me@desk"\n')
    hub = H.resolve_hub("lab", H.load_resolver())
    assert hub.host == HOST and hub.rows["host"].locked
    assert hub.holder == "me@desk"                     # a user key the admin did not set


def test_negative_twin_without_the_policy_the_users_file_decides():
    settings('[hubs.lab]\nhost = "mine.invalid"\n')
    assert H.resolve_hub("lab", H.load_resolver()).host == "mine.invalid"


def test_a_token_in_the_policy_is_dropped_with_a_problem(tmp_path, monkeypatch):
    policy(tmp_path, monkeypatch, MACHINE + 'token = "in-a-world-readable-file"\n')
    r = H.load_resolver()
    assert any("credential never goes in the policy" in p for p in r.problems)
    assert not H.resolve_hub("lab", r).token.get("set")


# --- the named hub's token (CCR SET-HUB-2) ---------------------------------------------------


def _named_cred(env=None, **kw):
    return hub_rest.resolve_credential(
        hub_rest.RestHubConfig(url="https://hub.invalid:7246", hub_name="remote",
                               settings_root=str(state())), env=env if env is not None else {},
        **kw)


def test_named_hub_token_comes_from_the_store():
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\n')
    H.set_hub_token("remote", H.load_resolver(), value="stored-token")
    cred = _named_cred()
    assert cred.header() == {"Authorization": "Bearer stored-token"}
    assert "hubs.remote.token" in cred.source and "stored-token" not in repr(cred)


def test_negative_twin_named_hub_env_beats_the_store_but_only_for_this_hub():
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\n')
    H.set_hub_token("remote", H.load_resolver(), value="stored-token")
    assert _named_cred({"FPGAHUB_TOKEN": "env-token"}).header()["Authorization"] == \
        "Bearer env-token"
    other = _named_cred({"FPGAHUB_TOKEN": "env-token", "FPGAHUB_ADDR": "elsewhere:7246"})
    assert other.header()["Authorization"] == "Bearer stored-token"


def test_named_hub_file_ref_is_read_strictly(tmp_path):
    tok = tmp_path / "hub.token"
    tok.write_text("file-token\n")
    tok.chmod(0o600)
    settings(f'[hubs.remote]\nurl = "https://hub.invalid:7246"\ntoken = "file:{tok}"\n')
    assert _named_cred().header()["Authorization"] == "Bearer file-token"


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_negative_twin_a_token_file_others_can_read_is_refused_for_a_named_hub(tmp_path):
    tok = tmp_path / "hub.token"
    tok.write_text("file-token\n")
    tok.chmod(0o644)
    settings(f'[hubs.remote]\nurl = "https://hub.invalid:7246"\ntoken = "file:{tok}"\n')
    with pytest.raises(UsageError) as ei:
        _named_cred()
    assert "readable by others" in ei.value.message and "file-token" not in ei.value.message
    with pytest.raises(UsageError):                    # and refused when it is set, too
        H.set_hub_token("remote", H.load_resolver(), ref=f"file:{tok}")


def test_named_hub_falls_back_to_the_fpgahub_login_for_this_hub(tmp_path):
    store = tmp_path / "login.toml"
    store.write_text('[client]\naddr = "hub.invalid:7246"\ntoken = "login-token"\n')
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\n')
    assert _named_cred(store=store).header()["Authorization"] == "Bearer login-token"
    store.write_text('[client]\naddr = "other.invalid:7246"\ntoken = "login-token"\n')
    assert not _named_cred(store=store).present              # twin: another hub's login


def test_a_token_in_an_unreachable_keyring_is_an_error_never_the_login_fallback(tmp_path):
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\n')
    r = H.load_resolver()
    r.secrets = SecretStore(state(), keyrings=[KeyringBackend("secret-service", env={})])
    r.secrets._write_index({"hubs.remote.token": {"backend": "secret-service"}})
    store = tmp_path / "login.toml"
    store.write_text('[client]\naddr = "hub.invalid:7246"\ntoken = "login-token"\n')
    with pytest.raises(UnreachableError) as ei:
        H.hub_credential("remote", "hub.invalid", 7246, resolver=r, login_store=store)
    assert ei.value.code == ExitCode.UNREACHABLE and "cannot reach" in ei.value.message


def test_the_inline_t8_order_is_unchanged_token_file_beats_env(tmp_path):
    tok = tmp_path / "t"
    tok.write_text("file-token\n")
    tok.chmod(0o600)
    cfg = hub_rest.parse_rest_table({"url": "https://hub.invalid:7246", "token_file": str(tok)})
    assert hub_rest.resolve_credential(cfg, env={"FPGAHUB_TOKEN": "env"}).header() == \
        {"Authorization": "Bearer file-token"}


def test_a_named_hubs_401_hint_names_the_hub_token_verb():
    cfg = hub_rest.RestHubConfig(url="https://hub.invalid:7246", hub_name="remote")
    err = hub_rest.hub_error("whoami", 401, {"detail": "nope"}, cfg)
    assert "hub token remote" in err.hint and "config set-secret hubs.remote.token" in err.hint
    inline = hub_rest.hub_error("whoami", 401, {"detail": "nope"},
                                hub_rest.RestHubConfig(url="https://hub.invalid:7246"))
    assert "token_file" in inline.hint and "fpgahub login" in inline.hint   # twin: T8's hint


# --- the hub's lease settings (CCR SET-HUB-3) and jump (CCR SET-HUB-4) -------------------------


class _Hub:
    def __init__(self, fake, cfg=None):
        self.host, self.target = HOST, "mps3_01_pl"
        self.client = hubmod.HubClient(HOST, "mps3_01_pl", runner=fake)
        if cfg is not None:
            self.config = cfg


def test_the_lease_takes_the_hubs_ttl_and_holder(tmp_path):
    from harness_manager.services.lease import LeaseService
    from tests.fakes.l1_fake_hub import FakeHub

    fake = FakeHub()
    try:
        cfg = hubmod.HubConfig(host=HOST, name="lab", holder="me@desk", lease_ttl=900)
        LeaseService(tmp_path / "s").acquire(_Hub(fake, cfg), board_id="b", heartbeat=False)
        acq = next(c for c in fake.calls if c[:3] == ["fpgahub", "lease", "acquire"])
        assert acq[acq.index("--ttl") + 1] == "900" and acq[acq.index("--holder") + 1] == "me@desk"
    finally:
        fake.close()


def test_negative_twin_without_a_named_hub_the_lease_keeps_its_defaults(tmp_path):
    from harness_manager.services.lease import DEFAULT_TTL_S, LeaseService, default_holder
    from tests.fakes.l1_fake_hub import FakeHub

    fake = FakeHub()
    try:
        LeaseService(tmp_path / "s").acquire(_Hub(fake), board_id="b", heartbeat=False)
        acq = next(c for c in fake.calls if c[:3] == ["fpgahub", "lease", "acquire"])
        assert acq[acq.index("--ttl") + 1] == str(DEFAULT_TTL_S)
        assert acq[acq.index("--holder") + 1] == default_holder()
    finally:
        fake.close()


def test_a_named_hubs_jump_reaches_the_hub_client_and_the_tunnel(monkeypatch):
    settings(LAB)
    boards(USE_LAB.replace('via = "hub"', f'via = "ssh:{HOST}"'))
    seen = []
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY",
                        lambda host, group, jump="": seen.append((host, group, jump)) or (lambda *a, **k: None))
    adapter = hubmod.adapter_for(cand())
    assert adapter.client.jump == "bastion.invalid" and seen == [(HOST, "fpga", "bastion.invalid")]
    assert T._hub_jump(T.with_via(cand(), f"ssh:{HOST}"), HOST) == "bastion.invalid"
    runner = hubmod.default_runner_factory(HOST, "fpga", jump="bastion.invalid")
    argv = runner.build(["fpgahub", "lease", "show", "mps3_01_pl"])
    assert argv[argv.index("-J") + 1] == "bastion.invalid" and argv[-2] == HOST
    assert "ConnectTimeout=15" in argv


def test_negative_twin_an_inline_hub_has_no_jump_and_pyverifys_argv_unchanged(monkeypatch):
    boards(f'[boards.lab]\nmatch = ["{ADDR}"]\nhub = {{ host = "{HOST}", target = "mps3_01_pl" }}\n')
    seen = []
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY",
                        lambda host, group: seen.append((host, group)) or (lambda *a, **k: None))
    hubmod.adapter_for(cand())
    assert seen == [(HOST, "fpga")]                    # the two-argument seam, as before
    assert T._hub_jump(cand(), HOST) == ""
    from pyverify.lease import SshHubRunner

    assert hubmod.default_runner_factory(HOST, "fpga") == SshHubRunner(HOST, group="fpga")


# --- add / token / remove ---------------------------------------------------------------------


def test_add_writes_a_hub_all_or_nothing_and_refuses_one_that_cannot_work():
    r = H.load_resolver()
    hub = H.add_hub("lab", {"transport": "ssh", "host": HOST, "lease_ttl": "20m"}, r)
    assert hub.lease_ttl == 1200
    text = (state() / "settings.toml").read_text()
    assert "[hubs.lab]" in text and 'lease_ttl = 1200' in text
    with pytest.raises(UsageError):
        H.add_hub("bad", {"transport": "ssh"}, r)       # no host
    with pytest.raises(UsageError):
        H.add_hub("bad", {"url": "http://off.invalid:7246"}, r)
    assert "hubs.bad" not in (state() / "settings.toml").read_text()


def test_negative_twin_add_refuses_an_existing_name_without_update():
    r = H.load_resolver()
    H.add_hub("lab", {"host": HOST}, r)
    with pytest.raises(UsageError) as ei:
        H.add_hub("lab", {"host": "x.invalid"}, r)
    assert "--update" in ei.value.hint
    assert H.add_hub("lab", {"host": "x.invalid"}, r, update=True).host == "x.invalid"


def test_remove_is_refused_while_a_board_uses_the_hub():
    settings(LAB)
    boards(USE_LAB)
    r = H.load_resolver()
    with pytest.raises(UsageError) as ei:
        H.remove_hub("lab", r)
    assert "lab" in ei.value.message and "--force" in ei.value.hint
    assert H.remove_hub("lab", r, force=True)["boards"] == ["lab"]
    with pytest.raises(AbsentError):
        H.remove_hub("lab", H.load_resolver())


# --- discovery: a board for a target ------------------------------------------------------------


DETAILS = {"description": "HBI0309C MPS3 #01 (lab)",
           "network": {"board_ip": ADDR, "hostname": "mps3-01-pl"}}


def test_discovery_writes_a_board_that_uses_the_hub_and_opens_through_it():
    settings(LAB)
    r = H.load_resolver()
    out = H.add_board_for_target("lab", "mps3_01_pl", r, details=DETAILS)
    assert out["board"] == "mps3_01_pl" and out["match"] == [ADDR]
    text = (state() / "boards.toml").read_text()
    assert 'hub = { use = "lab", target = "mps3_01_pl", shares = { mcc = ' \
           '"/dev/mps3_01_pl/tty_00" } }' in text and 'via = "hub"' in text
    cfg = hubmod.hub_config_for(cand())
    assert (cfg.name, cfg.target, cfg.shares) == ("lab", "mps3_01_pl",
                                                  {"mcc": "/dev/mps3_01_pl/tty_00"})


def test_negative_twin_discovery_refuses_a_second_board_for_the_same_target():
    settings(LAB)
    boards(USE_LAB)
    r = H.load_resolver()
    with pytest.raises(UsageError) as ei:
        H.add_board_for_target("lab", "mps3_01_pl", r, board_key="again", details=DETAILS)
    assert "already uses mps3_01_pl" in ei.value.message
    with pytest.raises(UsageError):
        H.add_board_for_target("lab", "kr260_01_ps", r, board_key="lab")   # the key is taken


# --- migration: "Make this a hub" -------------------------------------------------------------


INLINE = f'''# my boards (a comment that must survive)
[boards.lab]   # the lab board
match = ["{ADDR}"]
via = "ssh:{HOST}"   # through the hub
hub = {{ host = "{HOST}", target = "mps3_01_pl", shares = {{ mcc = "/dev/mps3_01_pl/tty_00" }} }}
power = {{ kind = "tasmota", url = "http://10.9.9.9" }}   # the plug
'''


def _essentials(cfg):
    return (cfg.host, cfg.target, cfg.shares, cfg.baud, cfg.start_shares, cfg.group, cfg.board)


def test_migration_round_trips_keeps_comments_and_backs_up():
    path = boards(INLINE)
    before = hubmod.hub_config_for(cand())
    out = H.adopt_inline_hub("lab", H.load_resolver())
    assert out["created"] and out["hub"] == "hub" and out["via"] == "hub"   # hub.invalid -> "hub"
    text = path.read_text()
    for kept in ("# my boards (a comment that must survive)", "# the lab board",
                 "# through the hub", "# the plug"):
        assert kept in text
    assert 'hub = { use = "hub", target = "mps3_01_pl", shares = { mcc = ' \
           '"/dev/mps3_01_pl/tty_00" } }' in text and 'via = "hub"' in text
    assert Path(out["backup"]).read_text() == INLINE
    after = hubmod.hub_config_for(cand())
    assert _essentials(after) == _essentials(before) and after.name == "hub"
    assert f'host = "{HOST}"' in (state() / "settings.toml").read_text()


def test_migration_is_idempotent():
    path = boards(INLINE)
    H.adopt_inline_hub("lab", H.load_resolver(), as_name="lab")
    first, first_settings = path.read_text(), (state() / "settings.toml").read_text()
    again = H.adopt_inline_hub("lab", H.load_resolver(), as_name="lab")
    assert not again["changed"] and again["hub"] == "lab"
    assert path.read_text() == first and (state() / "settings.toml").read_text() == first_settings


def test_migration_moves_a_token_file_to_a_file_ref_and_a_second_board_reuses_the_hub(tmp_path):
    tok = tmp_path / "hub.token"
    tok.write_text("t\n")
    tok.chmod(0o600)
    boards(f'[boards.a]\nmatch = ["10.0.0.1"]\n'
           f'hub = {{ url = "https://hub.invalid:7246", target = "x", token_file = "{tok}" }}\n'
           f'[boards.b]\nmatch = ["10.0.0.2"]\n'
           f'hub = {{ url = "https://hub.invalid:7246", target = "y", token_file = "{tok}" }}\n')
    a = H.adopt_inline_hub("a", H.load_resolver())
    b = H.adopt_inline_hub("b", H.load_resolver())
    assert a["created"] and b["reused"] and a["hub"] == b["hub"] == "hub"
    assert f'token = "file:{tok}"' in (state() / "settings.toml").read_text()
    assert H.boards_using(H.load_resolver(), "hub") == ["a", "b"]


def test_negative_twin_a_hub_name_taken_by_another_definition_is_refused_and_nothing_changes():
    settings('[hubs.lab]\nhost = "other.invalid"\n')
    path = boards(INLINE)
    with pytest.raises(UsageError) as ei:
        H.adopt_inline_hub("lab", H.load_resolver(), as_name="lab")
    assert "different definition" in ei.value.message and "--as NAME" in ei.value.hint
    assert path.read_text() == INLINE


def test_negative_twin_via_to_another_host_is_left_alone_and_a_table_style_hub_keeps_its_comments():
    path = boards(f'[boards.lab]\nmatch = ["{ADDR}"]\nvia = "ssh:jump.invalid"\n'
                  f'[boards.lab.hub]   # the hub table\nhost = "{HOST}"\n'
                  'target = "mps3_01_pl"   # the target\n')
    out = H.adopt_inline_hub("lab", H.load_resolver(), as_name="lab")
    text = path.read_text()
    assert out["via"] == "ssh:jump.invalid" and 'via = "ssh:jump.invalid"' in text
    assert "# the hub table" in text and "# the target" in text and 'use = "lab"' in text
    assert "host" not in text.split("[boards.lab.hub]")[1]
    assert json.dumps(hubmod.hub_config_for(cand()).name) == '"lab"'


def test_an_ssh_hub_takes_no_token():
    settings(LAB)
    with pytest.raises(UsageError) as ei:
        H.set_hub_token("lab", H.load_resolver(), value="x")
    assert "SSH key" in ei.value.message
    assert not (state() / "secrets").exists()


def test_negative_twin_a_rest_hub_takes_one_and_clear_forgets_it():
    settings('[hubs.remote]\nurl = "https://hub.invalid:7246"\n')
    r = H.load_resolver()
    assert H.set_hub_token("remote", r, value="x")["set"]
    assert not H.set_hub_token("remote", r, clear=True)["set"]


def test_the_daemon_and_cli_leave_the_ttl_to_the_hub_when_none_is_given():
    from harness_manager.cli.main import make_parser
    from harness_manager.daemon import hub_api

    assert hub_api._ttl({}) is None
    args = make_parser().parse_args(["lease", "acquire", "192.168.10.101"])
    assert (args.ttl, args.holder, args.timeout) == (None, None, None)
    assert make_parser().parse_args(["lease", "request", "192.168.10.101"]).ttl is None


def test_negative_twin_a_given_ttl_is_still_checked_and_used():
    from harness_manager.daemon import hub_api

    assert hub_api._ttl({"ttl_s": 600}) == 600
    for bad in (None, 5, "600", True):
        with pytest.raises(UsageError):
            hub_api._ttl({"ttl_s": bad})


def test_a_board_whose_hub_is_in_a_broken_settings_file_says_so():
    settings("[hubs.lab\nhost = 1\n")
    boards(USE_LAB)
    with pytest.raises(UsageError) as ei:
        hubmod.hub_config_for(cand())
    assert "no hub named 'lab'" in ei.value.message and "not valid TOML" in ei.value.message


# --- the declared rows: every key a board's hub table may hold (SET-PACK asked) ---------------


def _hub_fields(schema) -> set[str]:
    return {s.parts[3] for s in schema.rows if s.parts[:3] == ("boards", "*", "hub")
            and len(s.parts) >= 4}


def test_every_key_a_hub_table_takes_is_a_declared_row_core_or_pack():
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    r = H.load_resolver(engine=Engine(EngineConfig(state_dir=state())))
    schema = r.schema
    l1 = {"host", "target", "shares", "baud", "start_shares", "group", "board"}
    assert _hub_fields(schema) == l1 | set(hub_rest.REST_KEYS) | {"use"}
    sample = {"host": HOST, "url": "https://hub.invalid:7246", "group": "g",
              "token_file": "~/t", "ca_file": "~/ca", "cert_file": "~/c", "key_file": "~/k",
              "insecure": True, "events": False, "direct": "never", "timeout_s": 5,
              "target": "t1", "board": "b1", "shares": {"mcc": "/dev/t1/tty_00"},
              "baud": 9600, "start_shares": True}
    assert set(sample) | {"use"} == _hub_fields(schema)
    hubmod.parse_hub_table(sample)                          # the parser takes every one
    for key, value in sample.items():                       # and each passes its own row
        if key != "shares":
            assert r.check_settable(f"boards.x.hub.{key}", value)[1] == value


def test_negative_twin_an_undeclared_hub_key_is_refused_by_the_parser_and_is_no_row():
    with pytest.raises(UsageError, match="unknown keys: tokenfile"):
        hubmod.parse_hub_table({"host": HOST, "tokenfile": "~/t"})
    assert H.load_resolver().schema.find("boards.x.hub.tokenfile") is None


def test_with_the_engine_the_packs_rows_check_a_discovered_boards_keys():
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine

    settings(LAB)
    r = H.load_resolver(engine=Engine(EngineConfig(state_dir=state())))
    assert r.schema.find("boards.x.hub.target").pack == "mps3"
    assert H.load_resolver().schema.find("boards.x.hub.target") is None   # twin: core only
    out = H.add_board_for_target("lab", "mps3_01_pl", r, details=DETAILS)
    assert out["board"] == "mps3_01_pl"
