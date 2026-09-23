"""Team T5: which Engine the CLI uses, and the test hook. Each check has a twin."""

from __future__ import annotations

import json

import pytest

from harness_manager.cli import engine as cli_engine
from harness_manager.cli.engine import ENV_ENGINE, describe_engine, get_engine, set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode, UsageError
from harness_manager.core.services import Engine as EngineProtocol
from tests.fakes.t5_fake_engine import FakeEngine


@pytest.fixture(autouse=True)
def _no_factory(monkeypatch):
    monkeypatch.delenv(ENV_ENGINE, raising=False)
    previous = set_engine_factory(None)
    yield
    set_engine_factory(previous)


def test_default_is_the_installed_engine_and_it_satisfies_the_protocol():
    from harness_manager.engine import Engine

    eng = get_engine()
    try:
        assert isinstance(eng, Engine) and isinstance(eng, EngineProtocol)
    finally:
        eng.close_all()
    assert describe_engine() == "harness_manager.engine.Engine"


def test_the_fake_engine_satisfies_the_same_protocol():
    assert isinstance(FakeEngine(), EngineProtocol)


def test_a_factory_overrides_the_default_and_can_be_cleared():
    fake = FakeEngine()
    assert set_engine_factory(lambda _a: fake) is None
    assert get_engine() is fake and describe_engine() == "test factory"
    set_engine_factory(None)
    assert get_engine() is not fake


def test_env_names_a_factory(monkeypatch, capsys):
    monkeypatch.setenv(ENV_ENGINE, "tests.fakes.t5_fake_engine:make_engine")
    assert isinstance(get_engine(), FakeEngine)
    rc = main(["--json", "info", "127.0.0.1"])
    out, _ = capsys.readouterr()
    assert rc == 0 and json.loads(out)["candidate"]["label"] == "fake mps3 at 127.0.0.1"


@pytest.mark.parametrize("spec", ["no_colon_here", "tests.fakes.nosuch:make", "json:nosuch"])
def test_a_bad_env_factory_is_a_usage_error(monkeypatch, capsys, spec):
    monkeypatch.setenv(ENV_ENGINE, spec)
    with pytest.raises(UsageError):
        get_engine()
    assert main(["info", "127.0.0.1"]) == ExitCode.USAGE
    assert ENV_ENGINE in capsys.readouterr().err


def test_the_cli_never_constructs_an_engine_for_help_or_version(capsys):
    calls = []
    set_engine_factory(lambda _a: calls.append(1) or FakeEngine())
    assert main(["help", "--list"]) == 0 and main(["version"]) == 0
    assert calls == []
    assert main(["packs"]) == 0 and calls == [1]


def test_module_has_no_fallback_engine_left():
    # The mini engine was removed once T1's Engine merged: one engine, not two.
    assert not hasattr(cli_engine, "MiniEngine")
