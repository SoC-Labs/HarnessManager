"""Lane L6 bug pack: plain words for users, raw text for the log."""

from __future__ import annotations

import logging

SECRET = "KeyError: 'secret_internal_name'"


def test_a_job_bug_says_a_sentence_and_a_step_and_logs_the_raw_text(caplog):
    from harness_manager.core.events import EventBus
    from harness_manager.daemon.jobs import BoardGates, JobManager

    jobs = JobManager(EventBus(), BoardGates())

    def bug(_progress):
        raise KeyError("secret_internal_name")

    with caplog.at_level(logging.INFO):
        job = jobs.submit("deploy", "mps3@b", bug)
        assert job.finished.wait(10)
    jobs.shutdown(wait=True)
    err = job.error
    assert err["message"].startswith("Something went wrong in the background service")
    assert "KeyError" not in err["message"] and "secret_internal_name" not in err["message"]
    assert "restart the app" in err["hint"]
    assert "secret_internal_name" in caplog.text            # the raw text is in the log


def test_negative_twin_a_real_error_keeps_its_own_message():
    from harness_manager.core.errors import UnreachableError
    from harness_manager.core.events import EventBus
    from harness_manager.daemon.jobs import BoardGates, JobManager

    jobs = JobManager(EventBus(), BoardGates())

    def fail(_progress):
        raise UnreachableError("the board stopped answering")

    job = jobs.submit("deploy", "mps3@b", fail)
    assert job.finished.wait(10)
    jobs.shutdown(wait=True)
    assert job.error["message"] == "the board stopped answering"


def test_an_unhandled_request_error_returns_plain_words(caplog):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from harness_manager.daemon.app import create_app

    app = create_app(MagicMock(), token="t" * 32, static_dir=None)

    def boom():
        raise KeyError("secret_internal_name")

    app.add_api_route("/api/v1/_boom", boom)
    app.router.routes.insert(0, app.router.routes.pop())      # ahead of any catch-all

    with caplog.at_level(logging.ERROR), TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/_boom", headers={"Authorization": "Bearer " + "t" * 32})
    assert r.status_code == 500
    text = r.text
    assert "Something went wrong in the background service" in text
    assert "secret_internal_name" not in text and "KeyError" not in text
    assert "secret_internal_name" in caplog.text
