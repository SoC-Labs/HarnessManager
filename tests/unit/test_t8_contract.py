"""T8: the fake hub and the REST client against fpgahub v0.3.0 itself. Each check has a twin.

``tests/fakes/t8_fpgahub_v030_golden.json`` was recorded from fpgahub's OWN FastAPI app
(v0.3.0 source, real Bearer tokens, real LeaseManager, real audit journal) by
``tests/fakes/t8_record_fpgahub_golden.py``. It holds the OpenAPI schema of the routes
used and a transcript of 39 requests and the 16 events they caused. These tests:

1. replay the transcript against ``FakeFpgahub`` and compare every status, key, value
   and event (tokens and timestamps by type only);
2. check that every route the client calls and the fake serves exists in the schema;
3. validate the client's request bodies and the fake's typed responses against the
   schema's own component models.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from harness_manager.transports import hub_rest
from tests.fakes import t8_hub_rest as fakes

GOLDEN = json.loads((Path(fakes.__file__).with_name("t8_fpgahub_v030_golden.json")).read_text())
SCHEMA = GOLDEN["openapi"]
VOLATILE = {"token", "expires_at", "ts",
            # UI2-API-HUB: GET /status's times (compared by type, as expires_at)
            "lease_acquired_at", "lease_expires_at", "last_activity_at"}


# --- a small validator for the OpenAPI 3.1 subset pydantic emits ---------------------------------


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref:
        return SCHEMA["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return node


def validate(value: Any, node: dict[str, Any], where: str = "$") -> list[str]:
    node = _resolve(node)
    if "anyOf" in node:
        errs = [validate(value, alt, where) for alt in node["anyOf"]]
        return [] if any(not e for e in errs) else [f"{where}: matches no anyOf ({errs})"]
    if "allOf" in node:
        return [e for alt in node["allOf"] for e in validate(value, alt, where)]
    if "enum" in node and value not in node["enum"]:
        return [f"{where}: {value!r} not in {node['enum']}"]
    if "const" in node and value != node["const"]:
        return [f"{where}: {value!r} != {node['const']!r}"]
    t = node.get("type")
    checks = {"object": dict, "array": list, "string": str, "boolean": bool,
              "null": type(None)}
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{where}: {value!r} is not an integer"]
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{where}: {value!r} is not a number"]
    elif t in checks and not isinstance(value, checks[t]):
        return [f"{where}: {value!r} is not {t}"]
    errs: list[str] = []
    if "exclusiveMinimum" in node and isinstance(value, (int, float)) and \
            not value > node["exclusiveMinimum"]:
        errs.append(f"{where}: {value!r} <= {node['exclusiveMinimum']}")
    if isinstance(value, dict) and (t == "object" or "properties" in node):
        props = node.get("properties", {})
        errs += [f"{where}: missing {k!r}" for k in node.get("required", []) if k not in value]
        extra = node.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{where}.{k}")
            elif extra is False:
                errs.append(f"{where}: extra key {k!r}")
            elif isinstance(extra, dict):
                errs += validate(v, extra, f"{where}.{k}")
    if isinstance(value, list) and "items" in node:
        for i, v in enumerate(value):
            errs += validate(v, node["items"], f"{where}[{i}]")
    return errs


def response_schema(path: str, method: str, status: int) -> dict[str, Any] | None:
    op = SCHEMA["paths"][path][method.lower()]
    content = (op.get("responses", {}).get(str(status)) or {}).get("content") or {}
    schema = (content.get("application/json") or {}).get("schema")
    return schema if schema else None


def request_schema(path: str, method: str) -> dict[str, Any] | None:
    op = SCHEMA["paths"][path][method.lower()]
    body = op.get("requestBody")
    return body["content"]["application/json"]["schema"] if body else None


def template_of(path: str) -> str:
    """``/api/v1/targets/mps3_01_pl/lease`` -> ``/api/v1/targets/{name}/lease``."""
    if path in SCHEMA["paths"]:
        return path
    for tpl in SCHEMA["paths"]:
        if "{" in tpl and re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", tpl), path):
            return tpl
    raise KeyError(path)


# --- 1. the transcript --------------------------------------------------------------------------


def same_shape(golden: Any, got: Any, where: str = "$", key: str = "") -> list[str]:
    if isinstance(golden, dict):
        if not isinstance(got, dict):
            return [f"{where}: expected an object, got {got!r}"]
        errs = []
        if set(golden) != set(got):
            errs.append(f"{where}: keys {sorted(set(golden) ^ set(got))} differ")
        for k in set(golden) & set(got):
            errs += same_shape(golden[k], got[k], f"{where}.{k}", k)
        return errs
    if isinstance(golden, list):
        if not isinstance(got, list) or len(golden) != len(got):
            return [f"{where}: expected {len(golden)} items, got {got!r}"]
        return [e for i, (g, v) in enumerate(zip(golden, got, strict=True))
                for e in same_shape(g, v, f"{where}[{i}]", key)]
    if key in VOLATILE:
        return [] if type(golden) is type(got) else [f"{where}: type {type(got).__name__}"]
    return [] if golden == got and type(golden) is type(got) else \
        [f"{where}: expected {golden!r}, got {got!r}"]


def _replay(steps: list[dict[str, Any]]) -> tuple[list[tuple[dict, int, Any]], fakes.FakeFpgahub]:
    import http.client
    from urllib.parse import urlencode

    hub = fakes.FakeFpgahub().start()
    tokens = {"alice": hub.add_token("alice", "write", "alice-dev"),
              "bob": hub.add_token("bob", "write", "bob-dev"),
              "carol": hub.add_token("carol", "read", "carol-ro"),
              "david": hub.add_token("david", "admin", "david-admin")}
    mapped: dict[str, str] = {}
    out = []
    for step in steps:
        body = copy.deepcopy(step["body"])
        if isinstance(body, dict) and "token" in body:
            body["token"] = mapped[body["token"]]
        headers = {"Content-Type": "application/json"}
        if step["who"] == "bogus":
            headers["Authorization"] = "Bearer not-a-token"
        elif step["who"]:
            headers["Authorization"] = f"Bearer {tokens[step['who']]}"
        url = "/api/v1" + step["path"] + (f"?{urlencode(step['params'])}" if step["params"] else "")
        conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=10)
        conn.request(step["method"], url, body=None if body is None else json.dumps(body),
                     headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        got = json.loads(raw) if raw else None
        if isinstance(step["response"], dict) and isinstance(got, dict) and \
                isinstance(step["response"].get("token"), str):
            mapped[step["response"]["token"]] = got["token"]
        out.append((step, resp.status, got))
    return out, hub


@pytest.fixture(scope="module")
def replay():
    out, hub = _replay(GOLDEN["steps"])
    yield out, hub
    hub.close()


def test_the_fake_answers_every_recorded_request_as_fpgahub_did(replay):
    out, _ = replay
    problems = []
    for step, status, got in out:
        if status != step["status"]:
            problems.append(f"{step['name']}: status {status}, fpgahub said {step['status']}")
            continue
        problems += [f"{step['name']}: {e}" for e in same_shape(step["response"], got)]
    assert not problems, "\n".join(problems)
    assert len(out) == len(GOLDEN["steps"]) >= 39


def test_the_shape_check_catches_a_drifted_answer(replay):
    """Twin: a fake that dropped a key, renamed a kind or moved a position would fail."""
    out, _ = replay
    step, _, got = next(o for o in out if o[0]["name"] == "acquire_queued")
    drifted = dict(got, kind="waiting")
    assert same_shape(step["response"], drifted)
    assert same_shape(step["response"], {k: v for k, v in got.items() if k != "position"})
    assert same_shape(step["response"], dict(got, position=2))
    assert not same_shape(step["response"], got)


def _event_view(ev: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ev["type"], {k: v for k, v in ev["data"].items() if k not in VOLATILE}


def test_the_fake_emits_the_same_lease_events_in_the_same_order(replay):
    _, hub = replay
    want = [_event_view(e) for e in GOLDEN["events"] if e["type"].startswith("lease.")]
    got = [_event_view(e) for e in hub.events if e["type"].startswith("lease.")]
    assert got == want


def test_the_event_comparison_would_see_a_missing_admin_revoked_event(replay):
    _, hub = replay
    want = [_event_view(e) for e in GOLDEN["events"] if e["type"].startswith("lease.")]
    got = [_event_view(e) for e in hub.events
           if e["type"].startswith("lease.") and e["type"] != "lease.admin_revoked"]
    assert got != want


def test_the_real_hub_history_drops_by_and_reason_and_the_admin_revoked_record():
    """The fact T8's history merge exists for: measured on fpgahub's own app."""
    history = next(s for s in GOLDEN["steps"] if s["name"] == "history")["response"]["events"]
    kinds = [e["event"] for e in history]
    assert "lease.revoked" in kinds and "lease.admin_revoked" not in kinds
    assert all("by" not in e and "reason" not in e for e in history)
    admin = [e for e in GOLDEN["events"] if e["type"] == "lease.admin_revoked"]
    assert admin and admin[0]["data"]["by"] == "token:david" and "board" not in admin[0]["data"]


def test_the_history_fact_twin_a_record_with_by_would_be_noticed():
    history = next(s for s in GOLDEN["steps"] if s["name"] == "history")["response"]["events"]
    doctored = [dict(e, by="token:david") if e["event"] == "lease.revoked" else e
                for e in history]
    assert any("by" in e for e in doctored)


# --- 2. routes ------------------------------------------------------------------------------


def test_every_route_the_client_calls_is_in_the_v030_schema():
    for name, (method, path) in hub_rest.ROUTES.items():
        assert path in SCHEMA["paths"], f"{name}: {path} is not an fpgahub 0.3.0 route"
        assert method.lower() in SCHEMA["paths"][path], f"{name}: {method} {path}"


def test_a_route_fpgahub_does_not_have_is_caught():
    assert "/api/v1/targets/{name}/lease/revoke" not in SCHEMA["paths"]   # board-level only
    assert "delete" not in SCHEMA["paths"]["/api/v1/targets/{name}/lease/heartbeat"]


def test_every_route_the_fake_serves_is_in_the_v030_schema():
    for method, path in fakes.SERVED:
        assert path in SCHEMA["paths"] and method.lower() in SCHEMA["paths"][path], (method, path)


def test_the_revoke_route_is_admin_gated_in_the_schema_and_takes_reason_as_a_query():
    op = SCHEMA["paths"]["/api/v1/boards/{name}/lease/revoke"]["post"]
    assert [p["name"] for p in op["parameters"] if p["in"] == "query"] == ["reason"]
    assert "requestBody" not in op


def test_the_wait_route_takes_its_timeout_as_a_query_not_a_token():
    op = SCHEMA["paths"]["/api/v1/targets/{name}/lease/wait"]["get"]
    names = {p["name"]: p for p in op["parameters"]}
    assert "timeout" in names and names["token"].get("deprecated") is True


# --- 3. bodies ------------------------------------------------------------------------------


class _Recording(hub_rest.HttpTransport):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method, path, *, body=None, params=None, timeout):
        self.sent.append((method, path, body))
        return super().request(method, path, body=body, params=params, timeout=timeout)


def test_every_body_the_client_sends_validates_against_the_v030_models():
    with fakes.FakeFpgahub() as hub:
        ta, td = hub.add_token("alice"), hub.add_token("david", "admin")
        cfg = fakes.rest_config(hub)
        rec = _Recording(cfg, hub_rest.Credential(ta, "test"))
        a = hub_rest.RestHubClient(cfg, credential=rec.credential, http=rec,
                                   retry_backoff_s=(0.01,), wait_slice_s=0.2)
        lease, _ = a.lease_acquire("x", ttl=600)
        a.lease_heartbeat(lease.token, "x")
        a.share_start("/dev/mps3_01_pl/tty_02", 115200)          # a lane: never tty_00
        a.lease_release(lease.token, "x")
        a.lease_cancel()
        fakes.client_for(hub, td).lease_revoke("r")
        assert rec.sent
        checked = 0
        for method, path, body in rec.sent:
            tpl = template_of("/api/v1" + path)
            schema = request_schema(tpl, method)
            if body is None:
                continue
            assert schema is not None, f"{method} {tpl} takes no body, the client sent one"
            assert validate(body, schema) == [], (method, tpl, body)
            checked += 1
        assert checked >= 5


def test_a_body_fpgahub_would_refuse_fails_the_same_validation():
    share = request_schema("/api/v1/targets/{name}/shares", "POST")
    acquire = request_schema("/api/v1/targets/{name}/lease", "POST")
    assert validate({"tty_paths": ["/dev/x"], "holder": "me"}, share)      # extra="forbid"
    assert validate({"ttl_seconds": 0}, acquire)                            # gt=0
    assert validate({"ttl_seconds": 60, "tier": "urgent"}, acquire)          # LeaseTier enum
    assert not validate({"ttl_seconds": 60, "tier": "interactive"}, acquire)


def test_the_fake_typed_answers_validate_against_the_v030_response_models(replay):
    out, _ = replay
    checked = 0
    for step, status, got in out:
        tpl = template_of("/api/v1" + step["path"])
        schema = response_schema(tpl, step["method"], status) if status < 300 else None
        if schema is None:
            continue
        assert validate(got, schema) == [], (step["name"], validate(got, schema))
        checked += 1
    assert checked >= 8


def test_a_typed_answer_missing_a_required_field_fails_validation(replay):
    out, _ = replay
    step, status, got = next(o for o in out if o[0]["name"] == "whoami_write")
    schema = response_schema("/api/v1/whoami", "GET", status)
    assert validate({k: v for k, v in got.items() if k != "holder"}, schema)
