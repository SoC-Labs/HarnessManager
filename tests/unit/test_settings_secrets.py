"""SET-CORE: the secret store (david S2), spike S2 promoted and made hermetic.

The OS keyring is a stub here (``StubKeyring``). The real Secret Service path, in a private
D-Bus session with a throwaway GNOME Keyring, is ``tests/spikes/settings_keyring_real.py``
(never collected). No test here reaches a real keyring: ``tests/conftest.py`` sets
``HARNESS_MANAGER_KEYRING=off`` and every keyring below is a stub or has no bus.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time

import pytest

from harness_manager.core.errors import ExitCode, UnreachableError, UsageError
from harness_manager.settings import secrets as sec
from harness_manager.settings.secrets import (
    FileBackend,
    KeyringBackend,
    SecretStore,
    SecretValue,
    accepted,
    default_keyrings,
    file_name,
    parse_ref,
    resolve_secret,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
VALUE = "ghp_SECRET_VALUE_1234567890"


class StubKeyring:
    """The ``keyring`` package's three calls, in memory."""

    def __init__(self, *, slow_s: float = 0.0, refuse: bool = False) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.slow_s = slow_s
        self.refuse = refuse
        self.calls: list[str] = []

    def get_password(self, service, name):
        self.calls.append(f"get {service} {name}")
        time.sleep(self.slow_s)
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.calls.append(f"set {service} {name}")
        if self.refuse:
            raise RuntimeError("the collection is locked")
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        self.calls.append(f"delete {service} {name}")
        self.items.pop((service, name), None)


def reachable(stub=None, **kw) -> KeyringBackend:
    return KeyringBackend("secret-service", impl=stub or StubKeyring(), env={}, **kw)


def unreachable() -> KeyringBackend:
    """A real Secret Service backend in a process with no session bus (an ssh session)."""
    return KeyringBackend("secret-service", env={})


def no_import(monkeypatch):
    import importlib

    def boom(name, *a, **k):
        if name.startswith("keyring"):
            raise AssertionError(f"a keyring backend was imported: {name}")
        return real(name, *a, **k)
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", boom)


# --- the keyring when it is reachable ----------------------------------------------------------


def test_a_reachable_keyring_takes_the_secret_and_no_file_is_written(tmp_path):
    stub = StubKeyring()
    store = SecretStore(tmp_path, keyrings=[reachable(stub)])
    st = store.set("hubs.lab.token", VALUE)
    assert (st.set, st.backend, st.reachable, st.why) == (True, "secret-service", True, "")
    assert stub.items[("harness-manager", "hubs.lab.token")] == VALUE
    assert store.get("hubs.lab.token") == VALUE
    assert not FileBackend(tmp_path / "secrets").path("hubs.lab.token").exists()


def test_negative_twin_with_no_keyring_the_secret_goes_to_a_private_file(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    st = store.set("hubs.lab.token", VALUE)
    assert st.backend == "file" and st.why == "no keyring on this system"
    assert store.get("hubs.lab.token") == VALUE


# --- the 0600 file fallback --------------------------------------------------------------------


@posix_only
def test_the_file_fallback_is_0600_in_a_0700_dir_and_says_why(tmp_path):
    store = SecretStore(tmp_path, keyrings=[unreachable()])
    st = store.set("updates.github_token", VALUE)
    f = FileBackend(tmp_path / "secrets").path("updates.github_token")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert st.backend == "file" and "no session bus" in st.why
    assert store.get("updates.github_token") == VALUE


def test_the_index_names_where_never_what(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("updates.github_token", VALUE)
    store.set("hubs.lab.token", "tok-" + VALUE)
    text = (tmp_path / "secrets" / "index.json").read_text()
    idx = json.loads(text)
    assert set(idx["secrets"]) == {"updates.github_token", "hubs.lab.token"}
    assert {e["backend"] for e in idx["secrets"].values()} == {"file"}
    for leak in (VALUE, VALUE[:8], VALUE[-8:],
                 hashlib.sha256(VALUE.encode()).hexdigest()[:12]):
        assert leak not in text, leak

    # The length, as a value: a substring test tripped on a timestamp ("11:27:06Z").
    def numbers(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from numbers(v)
        elif isinstance(node, list):
            for v in node:
                yield from numbers(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            yield node
    assert len(VALUE) not in set(numbers(idx)) - {idx.get("version")}
    assert not any(k in {"length", "len", "size"} for e in idx["secrets"].values() for k in e)
    for st in (store.status("updates.github_token"), store.status("hubs.lab.token")):
        assert VALUE not in json.dumps(st.view()) and VALUE not in repr(st)


@posix_only
@pytest.mark.parametrize("loose", [0o640, 0o604, 0o644, 0o660])
def test_a_secret_file_others_can_read_is_refused(tmp_path, loose):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("updates.github_token", VALUE)
    f = FileBackend(tmp_path / "secrets").path("updates.github_token")
    os.chmod(f, loose)
    with pytest.raises(UsageError, match="readable by others") as exc:
        store.get("updates.github_token")
    assert "replace the secret" in exc.value.hint and VALUE not in str(exc.value)
    st = store.status("updates.github_token")
    assert st.set and not st.reachable and "readable by others" in st.why


@posix_only
def test_negative_twin_the_same_file_at_0600_is_read(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("updates.github_token", VALUE)
    f = FileBackend(tmp_path / "secrets").path("updates.github_token")
    os.chmod(f, 0o600)
    assert store.get("updates.github_token") == VALUE and store.status(
        "updates.github_token").reachable


@posix_only
def test_a_symlinked_secret_file_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(VALUE + "\n")
    os.chmod(target, 0o600)
    store = SecretStore(tmp_path, keyrings=[])
    store.set("hubs.lab.token", "placeholder")
    f = FileBackend(tmp_path / "secrets").path("hubs.lab.token")
    f.unlink()
    f.symlink_to(target)
    with pytest.raises(UsageError, match="symlink"):
        store.get("hubs.lab.token")


def test_file_names_are_safe_on_every_os():
    assert file_name("hubs.lab.token") == "hubs.lab.token.secret"
    assert file_name('boards."mps3@1.2:6900".power.auth.password') == \
        "boards.%22mps3%401.2%3A6900%22.power.auth.password.secret"
    assert file_name("Hubs.Lab.token").startswith("%48ubs.%4Cab")   # case-safe
    assert file_name(".hidden") == "%2Ehidden.secret"
    for bad in ("a/b", "a\\b", "", "a b", "x\n"):
        with pytest.raises(UsageError):
            sec.check_name(bad)


# --- stored, but not reachable here (7), is not "not set" --------------------------------------


def test_stored_in_a_keyring_this_process_cannot_reach_says_so_and_fails_with_7(tmp_path):
    desk = SecretStore(tmp_path, keyrings=[reachable()])        # the desktop session
    desk.set("hubs.lab.token", VALUE)
    ssh = SecretStore(tmp_path, keyrings=[unreachable()])       # the same user, over ssh
    st = ssh.status("hubs.lab.token")
    assert (st.set, st.backend, st.reachable) == (True, "secret-service", False)
    assert "no session bus" in st.why and "Secret Service" in st.where
    with pytest.raises(UnreachableError) as exc:
        ssh.get("hubs.lab.token")
    assert exc.value.code == ExitCode.UNREACHABLE == 7
    assert "cannot reach" in exc.value.message and "set the secret again" in exc.value.hint
    assert not FileBackend(tmp_path / "secrets").path("hubs.lab.token").exists()   # no copy


def test_negative_twin_never_stored_is_not_set_and_reads_none(tmp_path):
    ssh = SecretStore(tmp_path, keyrings=[unreachable()])
    st = ssh.status("hubs.lab.token")
    assert not st.set and st.backend == "" and ssh.get("hubs.lab.token") is None


def test_setting_it_again_where_the_keyring_is_unreachable_moves_it_to_the_file(tmp_path):
    SecretStore(tmp_path, keyrings=[reachable()]).set("hubs.lab.token", "old")
    ssh = SecretStore(tmp_path, keyrings=[unreachable()])
    st = ssh.set("hubs.lab.token", VALUE)
    assert st.backend == "file" and st.reachable and "no session bus" in st.why
    assert ssh.get("hubs.lab.token") == VALUE
    assert json.loads((tmp_path / "secrets" / "index.json").read_text())["secrets"][
        "hubs.lab.token"]["backend"] == "file"


def test_moving_back_to_the_keyring_deletes_the_file_copy(tmp_path):
    SecretStore(tmp_path, keyrings=[]).set("hubs.lab.token", "old")
    stub = StubKeyring()
    desk = SecretStore(tmp_path, keyrings=[reachable(stub)])
    assert desk.set("hubs.lab.token", VALUE).backend == "secret-service"
    assert not FileBackend(tmp_path / "secrets").path("hubs.lab.token").exists()


def test_a_keyring_that_refuses_to_store_is_unreachable_and_the_index_is_unchanged(tmp_path):
    store = SecretStore(tmp_path, keyrings=[reachable(StubKeyring(refuse=True))])
    with pytest.raises(UnreachableError, match="refused to store"):
        store.set("hubs.lab.token", VALUE)
    assert not store.status("hubs.lab.token").set


def test_delete_removes_it_everywhere(tmp_path):
    stub = StubKeyring()
    store = SecretStore(tmp_path, keyrings=[reachable(stub)])
    store.set("hubs.lab.token", VALUE)
    assert not store.delete("hubs.lab.token").set and not stub.items
    assert store.names() == []


def test_a_lost_index_still_finds_a_private_file(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("hubs.lab.token", VALUE)
    (tmp_path / "secrets" / "index.json").write_text("{not json")
    assert store.status("hubs.lab.token").set and store.get("hubs.lab.token") == VALUE
    assert "cannot be read" in store.problems[0]


@pytest.mark.parametrize("bad", ["", "  ", "TWO\nLINES", "WITH-CR\r", "X" * 20000])
def test_a_secret_is_one_non_empty_line_and_the_refusal_never_repeats_it(tmp_path, bad):
    with pytest.raises(UsageError, match="one non-empty line") as exc:
        SecretStore(tmp_path, keyrings=[]).set("hubs.lab.token", bad)
    for word in bad.split():
        assert word not in str(exc.value)


# --- the keyring guards ------------------------------------------------------------------------


def test_only_the_four_real_keyrings_are_accepted():
    def fake(module, name):
        return type(name, (), {"__module__": module})()
    assert accepted(fake("keyring.backends.SecretService", "Keyring"))[0] == "secret-service"
    assert accepted(fake("keyring.backends.Windows", "WinVaultKeyring"))[0] == \
        "windows-credential"
    assert accepted(fake("keyring.backends.macOS", "Keyring"))[0] == "macos-keychain"
    for module, name in (("keyrings.alt.file", "PlaintextKeyring"),
                         ("keyrings.alt.file", "EncryptedKeyring"),
                         ("keyring.backends.fail", "Keyring"),
                         ("keyring.backends.null", "Keyring"),
                         ("keyring.backends.chainer", "ChainerBackend")):
        assert accepted(fake(module, name)) is None, module


def test_with_no_session_bus_the_keyring_is_never_touched(monkeypatch):
    no_import(monkeypatch)
    k = KeyringBackend("secret-service", env={"PATH": "/usr/bin"})
    ok, why = k.available()
    assert not ok and "no session bus" in why


def test_the_keyring_switch_turns_it_off_without_touching_it(monkeypatch):
    no_import(monkeypatch)
    assert default_keyrings({"HARNESS_MANAGER_KEYRING": "off"}) == []
    k = KeyringBackend("secret-service", env={"HARNESS_MANAGER_KEYRING": "off",
                                              "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nope"})
    assert "turned off" in k.available()[1]


def test_negative_twin_each_os_gets_its_own_keyrings_and_building_them_touches_nothing(
        monkeypatch):
    no_import(monkeypatch)
    assert [k.name for k in default_keyrings({}, "linux")] == ["secret-service", "kwallet"]
    assert [k.name for k in default_keyrings({}, "darwin")] == ["macos-keychain"]
    assert [k.name for k in default_keyrings({}, "win32")] == ["windows-credential"]


def test_a_keyring_that_does_not_answer_in_time_counts_as_unreachable(tmp_path):
    slow = reachable(StubKeyring(slow_s=1.0), probe_timeout_s=0.1)
    ok, why = slow.available()
    assert not ok and "did not answer" in why
    backend, why = SecretStore(tmp_path, keyrings=[slow]).where_new()
    assert backend.name == "file" and "did not answer" in why


def test_the_test_suite_never_reaches_a_real_keyring():
    assert os.environ.get("HARNESS_MANAGER_KEYRING") == "off"
    assert default_keyrings() == []


# --- the lookup order: env, the store, then legacy sources -------------------------------------


def test_env_beats_the_store_and_the_store_beats_legacy(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    store.set("updates.github_token", "from-store")
    legacy = [("gh", lambda: "from-gh")]
    got = resolve_secret("updates.github_token", store=store, legacy=legacy,
                         env={"HARNESS_MANAGER_GITHUB_TOKEN": "from-env"},
                         env_var="HARNESS_MANAGER_GITHUB_TOKEN")
    assert got.value == "from-env" and got.source == "$HARNESS_MANAGER_GITHUB_TOKEN"
    got = resolve_secret("updates.github_token", store=store, legacy=legacy, env={},
                         env_var="HARNESS_MANAGER_GITHUB_TOKEN")
    assert got.value == "from-store" and got.source == "a private file"
    store.delete("updates.github_token")
    got = resolve_secret("updates.github_token", store=store, legacy=legacy, env={})
    assert got.value == "from-gh" and got.source == "gh"
    assert resolve_secret("updates.github_token", store=store, env={}) is None


def test_env_ok_limits_the_variable_to_its_hub(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    env = {"FPGAHUB_TOKEN": "tok", "FPGAHUB_ADDR": "other:7246"}
    got = resolve_secret("hubs.lab.token", store=store, env=env, env_var="FPGAHUB_TOKEN",
                         env_ok=lambda e: e.get("FPGAHUB_ADDR", "") in ("", "lab:7246"))
    assert got is None
    got = resolve_secret("hubs.lab.token", store=store, env={"FPGAHUB_TOKEN": "tok"},
                         env_var="FPGAHUB_TOKEN",
                         env_ok=lambda e: e.get("FPGAHUB_ADDR", "") in ("", "lab:7246"))
    assert got.value == "tok"


def test_an_unreachable_store_is_never_quietly_replaced_by_a_legacy_source(tmp_path):
    SecretStore(tmp_path, keyrings=[reachable()]).set("updates.github_token", VALUE)
    ssh = SecretStore(tmp_path, keyrings=[unreachable()])
    with pytest.raises(UnreachableError):
        resolve_secret("updates.github_token", store=ssh, env={},
                       legacy=[("gh", lambda: "from-gh")])


@posix_only
def test_a_reference_to_a_file_or_a_variable(tmp_path):
    store = SecretStore(tmp_path, keyrings=[])
    f = tmp_path / "hub.token"
    f.write_text("tok-file\n")
    os.chmod(f, 0o600)
    got = resolve_secret("hubs.lab.token", store=store, env={}, ref=f"file:{f}")
    assert got.value == "tok-file"
    os.chmod(f, 0o644)
    with pytest.raises(UsageError, match="readable by others"):
        resolve_secret("hubs.lab.token", store=store, env={}, ref=f"file:{f}")
    got = resolve_secret("hubs.lab.token", store=store, env={"MY_TOK": "tok-env"},
                         ref="env:MY_TOK")
    assert got.value == "tok-env" and got.source == "$MY_TOK"


def test_a_reference_is_never_a_pasted_secret_and_the_refusal_never_repeats_it():
    assert parse_ref("store") == ("store", "") and parse_ref(None) == ("store", "")
    assert parse_ref("file:~/t") == ("file", "~/t") and parse_ref("gh") == ("gh", "")
    with pytest.raises(UsageError) as exc:
        parse_ref(VALUE)
    assert VALUE not in str(exc.value) and "set-secret" in exc.value.hint


def test_a_secret_value_never_shows_in_repr_or_str():
    v = SecretValue(VALUE, "a private file")
    assert VALUE not in repr(v) and VALUE not in str(v) and v.value == VALUE
