"""DEMO-ALL: the showcase (``harness-manager app --demo``) shows every feature, offline.

The real daemon app over ``DemoEngine(showcase=True)`` (what ``--demo`` serves), driven
through its API with the network cut: every socket connect to a non-loopback address, every
DNS lookup of a non-loopback name and every ``ssh``/``gh``/``fpgahub``/``hw_server`` start is
recorded and refused. Each board's capability lines are checked against what it is for, and
each check has a negative twin (the board that must NOT show it, or the guard catching the
real product going on the network where the demo does not).
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager import demo_catalog as cat
from harness_manager.core.errors import ExitCode, HarnessError
from harness_manager.daemon.app import create_app
from harness_manager.demo import DemoEngine
from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.fakes.kit_fakes import FIXTURE

TOKEN = "demo-all"
H = {"Authorization": f"Bearer {TOKEN}"}
BLOCKED = ("ssh", "scp", "gh", "fpgahub", "hw_server", "sg", "vivado")


# --- the network cut ----------------------------------------------------------------------------


class NetworkCut(OSError):
    pass


def _loopback(host: Any) -> bool:
    h = str(host or "")
    return h in ("localhost", "::1", "") or h.startswith("127.")


@pytest.fixture
def offline(monkeypatch):
    """Refuse (and record) every way off this machine; the list is the evidence. The tests'
    local update source (tests/conftest.py) is taken away: the product's default is GitHub."""
    monkeypatch.delenv("HARNESS_MANAGER_UPDATE_SOURCE", raising=False)
    attempts: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_popen = subprocess.Popen.__init__

    def addr_host(address: Any) -> Any:
        return address[0] if isinstance(address, tuple) else address

    def connect(self, address):                               # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(addr_host(address)):
            attempts.append(f"connect {address!r}")
            raise NetworkCut(f"the test cut the network: connect {address!r}")
        return real_connect(self, address)

    def connect_ex(self, address):                            # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(addr_host(address)):
            attempts.append(f"connect_ex {address!r}")
            return 101                                        # ENETUNREACH
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kw):                       # noqa: ANN001
        if not _loopback(host):
            attempts.append(f"getaddrinfo {host!r}")
            raise socket.gaierror(f"the test cut the network: {host!r}")
        return real_getaddrinfo(host, *args, **kw)

    def popen(self, args, *a, **kw):                          # noqa: ANN001
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        name = Path(str(argv[0]).split()[0]).name if argv else ""
        if name in BLOCKED:
            attempts.append(f"exec {name}")
            raise NetworkCut(f"the test cut the network: exec {name}")
        return real_popen(self, args, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(subprocess.Popen, "__init__", popen)
    return attempts


# --- the demo over the real daemon app ------------------------------------------------------------


@pytest.fixture
def showcase(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "not-the-demo"))
    monkeypatch.delenv(cat.UPDATE_ENV, raising=False)
    sdir = tmp_path / "demo"
    eng = DemoEngine(speed=0, showcase=True, state_dir=sdir)
    try:
        with TestClient(create_app(eng, token=TOKEN, state_dir=sdir)) as client:
            yield Demo(client, eng)
    finally:
        eng.close_all()


class Demo:
    def __init__(self, client: TestClient, engine: DemoEngine) -> None:
        self.c, self.engine = client, engine
        self.cands = {c["board_id"]: c for c in
                      self.get("/probe", method="POST", json={})["candidates"]}

    def get(self, path: str, *, method: str = "GET", status: int | None = 200,
            **kw: Any) -> dict[str, Any]:
        r = self.c.request(method, f"/api/v1{path}", headers=H, **kw)
        if status is not None:
            assert r.status_code == status, (path, r.status_code, r.text)
        return r.json()

    def b(self, bid: str) -> str:
        return f"/boards/{quote(bid, safe='')}"

    def open(self, bid: str) -> dict[str, Any]:
        self.get("/boards", method="POST", json={"candidate": self.cands[bid]})
        return self.get(self.b(bid))

    def job(self, path: str, body: Any = None, *, ok: bool = True) -> dict[str, Any]:
        r = self.c.post(f"/api/v1{path}", headers=H, json=body or {})
        assert r.status_code == 202, (path, r.status_code, r.text)
        jid = r.json()["job"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            j = self.get(f"/jobs/{jid}")
            if j["state"] not in ("queued", "running"):
                assert (j["state"] == "done") == ok, j
                return j
            time.sleep(0.02)
        raise AssertionError(f"job {jid} did not end: {j}")


# --- the three boards: what each one shows, and what the others must not -------------------------


def test_three_showcase_boards_each_a_different_harness(showcase):
    # ... and LEASE-UI's spare (mps3-03): a second board behind the hub, its lease free
    assert list(showcase.cands) == [BOARD_LINUX, BOARD_V011, BOARD_LEASED, BOARD_SPARE]
    impl = {bid: showcase.open(bid)["identity"]["harness_impl"] for bid in showcase.cands}
    assert impl == {BOARD_LINUX: "linux", BOARD_V011: "bare-metal", BOARD_LEASED: "bare-metal",
                    BOARD_SPARE: "bare-metal"}
    names = {bid: c["name"] for bid, c in showcase.cands.items()}
    assert names == {BOARD_LINUX: "mps3-lx", BOARD_V011: "mps3-01", BOARD_LEASED: "mps3-02",
                     BOARD_SPARE: "mps3-03"}


def test_the_linux_board_shows_card_slots_claim_panel_identify_and_xvc(showcase):
    info = showcase.open(BOARD_LINUX)
    ident = info["identity"]
    assert {"usd", "presence", "panel", "locate", "xvc_lock"} <= set(ident["features"])
    assert {"front_panel", "locate", "presence", "debug_fabric", "console_shell"} <= \
        set(info["capabilities"])
    assert info["claim"]["state"] == "mine" and info["claim"]["claimed"]["mine"] is True
    B = showcase.b(BOARD_LINUX)
    card = showcase.get(f"{B}/card")
    assert card["line"] == "valid · default nanosoc [A] · OS A:valid* B:valid"
    assert card["card"]["present"] and card["card"]["default"]["slot"] == "A"
    slots = showcase.get(f"{B}/slots")
    assert slots["available"] and set(slots["slots"]["slots"]) == {"A", "B"}
    panel = showcase.get(f"{B}/panel")
    assert panel["panel"]["source"] == "panel" and panel["identify"]["available"]
    assert panel["panel"]["touch"]["ok"] is True and len(panel["panel"]["sessions"]) == 2
    assert showcase.get(f"{B}/identify", method="POST", json={"seconds": 3})["seconds"] == 3
    xvc = showcase.get(f"{B}/xvc")
    assert xvc["state"] == "down" and not xvc["reason"] and xvc["warnings"] == []
    out = showcase.job(f"{B}/xvc/open")["result"]
    assert out["state"] == "ready" and out["reach"] == "board-ssh" and out["url"]
    assert showcase.get(f"{B}/xvc/ltx?which=static&format=json")["name"].endswith(".ltx")


def test_negative_twin_the_bare_metal_board_has_none_of_the_linux_lines(showcase):
    info = showcase.open(BOARD_V011)
    assert "claim" not in info                                   # no SSH to claim
    assert info["unavailable"]["locate"].startswith("Identify isn't available on this harness "
                                                    "image yet")
    B = showcase.b(BOARD_V011)
    assert showcase.get(f"{B}/card")["line"].startswith("n/a: this harness has no microSD")
    assert showcase.get(f"{B}/slots")["available"] is False
    panel = showcase.get(f"{B}/panel")
    assert panel["panel"]["source"] == "rebuilt" and not panel["identify"]["available"]
    frame = showcase.get(f"{B}/panel/frame")
    assert frame["source"] == "rebuilt" and frame["rows"][0].startswith("MPS3-01")
    r = showcase.get(f"{B}/identify", method="POST", json={"seconds": 3}, status=422)
    assert r["error"]["code"] == ExitCode.UNAVAILABLE
    xvc = showcase.get(f"{B}/xvc")
    assert any("unauthenticated" in w for w in xvc["warnings"])       # the XVC warning
    assert showcase.job(f"{B}/xvc/open")["result"]["state"] == "ready"
    # ...and the Debug USB features the Linux board lacks
    assert {"console_controller", "storage_backup", "reboot_board"} <= set(info["capabilities"])
    linux = showcase.open(BOARD_LINUX)
    assert linux["unavailable"]["console_controller"] == "needs the Debug USB cable"


def test_the_leased_board_shows_alice_the_queue_your_request_and_force(showcase):
    showcase.open(BOARD_LEASED)
    B = showcase.b(BOARD_LEASED)
    view = showcase.get(f"{B}/lease")
    assert view["lease"]["holder"] == "alice@lab-pc-07" and not view["lease"]["mine"]
    assert view["lease"]["holder_kind"] == "unknown"                   # D12: type the name
    assert [q["position"] for q in view["queue"]] == [1, 2] and view["queue"][0]["mine"]
    assert view["request"]["force_available"] is True
    assert showcase.get(f"{B}/tunnel")["tunnel"]["state"] == "up"
    # Negative twin: XVC is for the lease holder only, and the refusal names alice.
    r = showcase.get(f"{B}/xvc/open", method="POST", json={}, status=409)
    assert r["error"]["name"] == "HELD" and "alice" in r["error"]["message"]
    # Force-release (the board's name typed, D12) makes it yours; bob stays queued.
    showcase.job(f"{B}/lease/force", {"confirm": True, "confirm_board": "mps3-02"})
    after = showcase.get(f"{B}/lease")
    assert after["lease"]["mine"] and [q["holder"] for q in after["queue"]] == ["bob@lab-pc-03"]


# --- LEASE-UI: the spare board (a free lease), and DELETE /boards/{bid}?release=true ---------------


def _spare_held_here(showcase: Demo) -> str:
    showcase.open(BOARD_SPARE)
    B = showcase.b(BOARD_SPARE)
    view = showcase.get(f"{B}/lease")
    assert view["hub"] and view["lease"] is None and view["queue"] == []      # free
    showcase.job(f"{B}/lease")                                                 # Acquire
    lease = showcase.get(f"{B}/lease")["lease"]
    assert lease["here"] and lease["mine"] and lease["target"] == "mps3_03_pl"
    return B


def test_close_with_release_gives_the_lease_back_before_closing(showcase):
    B = _spare_held_here(showcase)
    out = showcase.get(f"{B}?release=true", method="DELETE")
    assert out["closed"] is True and out["released"]["target"] == "mps3_03_pl"
    assert BOARD_SPARE not in showcase.engine.open_boards()
    showcase.open(BOARD_SPARE)
    assert showcase.get(f"{B}/lease")["lease"] is None                         # free again


def test_negative_twin_a_plain_close_keeps_the_lease(showcase):
    B = _spare_held_here(showcase)
    out = showcase.get(B, method="DELETE")
    assert out["closed"] is True and "released" not in out                     # as before
    showcase.open(BOARD_SPARE)
    lease = showcase.get(f"{B}/lease")["lease"]
    assert lease is not None and lease["here"]                                 # still ours


def test_close_with_release_leaves_someone_elses_lease_alone(showcase):
    showcase.open(BOARD_LEASED)
    B = showcase.b(BOARD_LEASED)
    out = showcase.get(f"{B}?release=true", method="DELETE")
    assert out["closed"] is True and out["released"] is None                   # nothing held here
    showcase.open(BOARD_LEASED)
    assert showcase.get(f"{B}/lease")["lease"]["holder"] == "alice@lab-pc-07"


def test_close_release_must_be_true_or_false_and_a_bad_one_closes_nothing(showcase):
    B = _spare_held_here(showcase)
    r = showcase.get(f"{B}?release=maybe", method="DELETE", status=400)
    assert r["error"]["name"] == "USAGE"
    assert BOARD_SPARE in showcase.engine.open_boards()
    assert showcase.get(f"{B}/lease")["lease"]["here"]


def test_the_leased_boards_mcc_is_reached_on_the_hub_never_a_tty_00_share(showcase):
    # MCC-FIX, in the demo's shape (INTEG-W4): the MCC is a HUB link the pack's mcc_link makes
    # (hub-mcc://HOST/TARGET/dev/TARGET/tty_00, via hub), no hub:// share link, no MCC console
    links = showcase.cands[BOARD_LEASED]["links"]
    (mcc,) = [lk for lk in links if lk["kind"] == "hub"]
    assert mcc["address"] == ("hub-mcc://mapstone-dev.ecs.soton.ac.uk/mps3_02_pl"
                              "/dev/mps3_02_pl/tty_00")
    assert mcc["via"] == "hub" and "never an fpgahub share" in mcc["detail"]
    assert not any(lk["address"].startswith("hub://") for lk in links)
    showcase.open(BOARD_LEASED)
    B = showcase.b(BOARD_LEASED)
    assert "mcc" not in showcase.get(f"{B}/consoles")["names"]
    temps = [r for r in showcase.get(f"{B}/telemetry")["readings"] if r["name"] == "mcc_temp"]
    assert temps and temps[0]["source"] == "mcc-console (hub)"
    info = showcase.get(B)
    assert {"console_controller", "reboot_board"} <= set(info["capabilities"])


def test_negative_twin_the_debug_usb_boards_mcc_is_its_own_usb_and_the_reboot_names_the_bit(
        showcase):
    links = showcase.cands[BOARD_V011]["links"]
    assert not any(lk["kind"] == "hub" for lk in links)
    assert any(lk["kind"] == "usb_serial" for lk in links)
    showcase.open(BOARD_V011)
    B = showcase.b(BOARD_V011)
    ev = showcase.job(f"{B}/controller/reboot", {})["result"]
    # MCC-FIX's evidence: the .bit the MCC said it loaded (the UI's "MCC loaded ..." line)
    assert ev["fpga_file"] == "MB/HBI0309C/Nanosoc/nanosoc.bit" and ev["fpga_configured"]


def test_the_linux_boards_slots_carry_confirmed_and_claimed(showcase):
    # LINUX-ANSWERS' additive fields (S2/S5), as the demo's Linux harness reports them
    from harness_manager.services import slot_health

    showcase.open(BOARD_LINUX)
    slots = showcase.get(f"{showcase.b(BOARD_LINUX)}/slots")["slots"]
    assert slots["confirmed"] is True and slots["claimed"] is True
    assert slots["fell_back"] is None and slots["committed_unbooted"] is None
    assert slots["slots"]["A"]["boot"] == "booted, confirmed healthy"
    assert slots["slots"]["B"]["boot"] == "read back this boot"
    assert any("SSH is claimed" in n for n in slots["notes"])
    assert not any("does not report" in n for n in slots["notes"])
    # twin: without the fields (a harness that does not send them) the same slots read
    # "booted (not yet confirmed)": the demo carries them, the product did not change
    st = showcase.engine.session(BOARD_LINUX).os_slots.status()
    bare = dataclasses.replace(st, raw={})
    assert slot_health.boot_words(bare, "A") == "booted (not yet confirmed)"
    assert slot_health.claimed(bare) is None


def test_negative_twin_boards_not_behind_a_hub_have_no_lease(showcase):
    for bid in (BOARD_LINUX, BOARD_V011):
        showcase.open(bid)
        view = showcase.get(f"{showcase.b(bid)}/lease")
        assert view["lease"] is None and view["hub"] is None


# --- the catalogue, the kit, the app update ----------------------------------------------------------


def _catalog(showcase: Demo, bid: str) -> dict[str, dict[str, Any]]:
    out = showcase.job("/harness/catalog/refresh", {"board_id": bid, "all": True})["result"]
    return {r["version"]: r for r in out["releases"]}


def test_the_catalogue_gives_the_bare_metal_board_every_verdict_a_history_and_a_pin(showcase):
    showcase.open(BOARD_V011)
    rows = _catalog(showcase, BOARD_V011)
    assert {v: r["verdict"] for v, r in rows.items()} == {
        "2.0.0": "needs-door", "1.1.1": "fits", "1.1.0": "fits", "1.0.0": "re-key"}
    assert rows["1.1.0"]["running"] and rows["1.1.1"]["pinned"]
    hist = showcase.get(f"{showcase.b(BOARD_V011)}/harness/history")
    assert [h["version"] for h in hist["history"]] == ["1.1.0", "1.0.0"]
    assert hist["pinned"] == cat.PIN


def test_negative_twin_the_linux_board_runs_the_beta_and_is_not_pinned(showcase):
    showcase.open(BOARD_LINUX)
    rows = _catalog(showcase, BOARD_LINUX)
    assert rows["2.0.0"]["running"] and rows["2.0.0"]["verdict"] == "fits"
    assert not any(r["pinned"] for r in rows.values())
    assert rows["1.1.1"]["verdict"] == "needs-door"                 # no Debug USB on it


def test_the_demo_kit_is_the_committed_fixture_and_both_boards_have_one(showcase):
    kit = showcase.engine.catalog.kit_dir
    for rel in ("static/static_routed_locked.dcp", "static/static_stamp.json"):
        assert (kit / rel).read_bytes() == (FIXTURE / rel).read_bytes(), rel
    ours, theirs = (json.loads((d / "kit.json").read_text()) for d in (kit, FIXTURE))
    assert {k for k in theirs if theirs[k] != ours.get(k)} == {"generated_by", "source"}
    assert {k["static_id"] for k in showcase.get("/kits")["kits"]} == {cat.S_ILA, cat.S_LNX}
    showcase.open(BOARD_V011)
    steps = {s["id"]: s["state"] for s in showcase.get(f"{showcase.b(BOARD_V011)}/guide")["steps"]}
    assert steps["target"] == "done" and steps["kit"] == "done"


def test_negative_twin_the_kit_dcp_carries_its_own_static(showcase):
    import zlib

    lnx = showcase.engine.catalog.linux_kit_dir / "static" / "static_routed_locked.dcp"
    ila = showcase.engine.catalog.kit_dir / "static" / "static_routed_locked.dcp"
    assert zlib.crc32(lnx.read_bytes()) == int(cat.S_LNX, 16)
    assert zlib.crc32(ila.read_bytes()) == int(cat.S_ILA, 16) != zlib.crc32(lnx.read_bytes())


def test_the_app_update_banner_is_off_by_default(showcase):
    st = showcase.get("/update/app")
    assert st["staged"] == [] and st["dev_install"]              # the banner's two "no"s


def test_negative_twin_the_knob_stages_a_pretend_update_that_never_applies(tmp_path, monkeypatch):
    monkeypatch.setenv(cat.UPDATE_ENV, "staged")
    sdir = tmp_path / "demo"
    eng = DemoEngine(speed=0, showcase=True, state_dir=sdir)
    try:
        app = create_app(eng, token=TOKEN, state_dir=sdir)
        app.state.daemon.runtime = {"port": 1, "listen": "127.0.0.1", "demo": True}
        with TestClient(app) as c:
            st = c.get("/api/v1/update/app", headers=H).json()
            assert st["staged"] == [cat.staged_version()] and not st["dev_install"]
            r = c.post("/api/v1/update/app/apply", headers=H, json={"confirm": True})
            assert r.status_code == 409 and "demo" in r.json()["error"]["message"]
    finally:
        eng.close_all()


def test_the_classic_engine_is_unchanged():
    eng = DemoEngine(speed=0)
    try:
        assert [c.board_id for c in eng.probe()][0].endswith("192.168.10.101:6900")
        for attr in ("xvc", "board_claim", "update", "kit_channel", "state_dir"):
            assert not hasattr(eng, attr), attr
    finally:
        eng.close_all()


def test_a_temporary_state_dir_is_removed_when_the_engine_closes():
    eng = DemoEngine(speed=0, showcase=True)
    sdir = eng.state_dir
    assert (sdir / "demo-fixtures" / "www" / "channel" / "stable" / "channel.json").is_file()
    eng.close_all()
    assert not sdir.exists()


# --- offline: every route above, with the network cut --------------------------------------------


def test_the_demo_never_touches_the_network(offline, showcase):
    for bid in showcase.cands:
        showcase.open(bid)
        B = showcase.b(bid)
        for path in ("/card", "/slots", "/panel", "/panel/frame", "/xvc", "/xvc/tcl", "/lease",
                     "/tunnel", "/kit", "/guide", "/harness/history", "/overlays", "/consoles",
                     "/telemetry"):
            showcase.get(B + path, status=None)
        _catalog(showcase, bid)
        showcase.get("/update/check", method="POST", json={"board_id": bid}, status=None)
    showcase.get("/update/app")
    showcase.job("/kits/fetch", {"static_id": cat.S_LNX})
    showcase.job(f"{showcase.b(BOARD_LEASED)}/lease/force",
                 {"confirm": True, "confirm_board": "mps3-02"})
    showcase.job(f"{showcase.b(BOARD_LINUX)}/xvc/open")
    assert offline == []


def test_negative_twin_the_cut_catches_the_real_update_service(offline, tmp_path):
    """The same cut sees the product go on the network: the real update service with its
    default (GitHub) source. So an empty list above means the demo stayed home."""
    from harness_manager.services.update import UpdateService

    svc = UpdateService(state_dir=tmp_path / "real", token="")
    with pytest.raises(HarnessError):
        svc.fetch_channel("stable", None, catalog="mps3-harness")
    assert offline and any("github" in a for a in offline)
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 9), timeout=1)
