"""Spike (lane HARNESS-DIST): a fake multi-version release, end to end, against T7.

Run it (board-free; everything is 127.0.0.1 and /tmp/hdist-*; nothing is left behind)::

    PYTHONPATH=src:. python -m tests.spikes.harness_dist_spike

What it does:

1. Makes four fake mints with REAL-format parts: an SD tree whose ``nanosoc.bit``
   has a real Xilinx header (part, UserID), overlay triples from T2's fixture
   keyed to each static, a Linux slot image, and a DUT kit zip.
2. Publishes them with the prototype release command (``harness_dist_publish``),
   signed with a THROWAWAY key generated in memory, into a local "web" served
   by T7's ``FakeChannelServer`` on 127.0.0.1: ``stable`` (1.0.0, 1.1.0, 1.1.1)
   and ``beta`` (+ 2.0.0, Linux).
3. Runs T7's own code (``UpdateService``) against a VirtualMps3 (FakeShell +
   FakeMcc + FakeSdVolume, the real MPS3 pack) running the fielded ILA static
   0x72BB0A36 with firmware v0.11: list, per-version plans, installs, a
   downgrade across statics, the SD-backup rollback.
4. Probes the gaps a real multi-version, multi-board catalogue hits.

Each probe prints PASS (T7 does it), GAP (T7 does not, and the design needs
it) or NOTE, with the evidence. The design doc quotes this output.

OTA-C (2026-09-24) re-ran it after closing P7-P10: P9 now plays GitHub's API host on
127.0.0.1 (``token_hosts``, as the unit tests do; the twin shows a non-GitHub host never
gets the token), and P10 builds its mirror with ``mirror.write_mirror`` (the designed
``harness mirror`` flow) instead of a plain copy (kept as P10b, which still fails). The
bare-metal fake mints now record ``ver32 = 0x01000000``, what the v0.11 firmware (VERSION
1.0.0) reports, since HM matches and confirms ``ver32`` when both sides have one (H1).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HarnessError
from harness_manager.core.model import BoardIdentity
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update import UpdateService, minisign
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.state import UpdateState
from harness_manager.services.update.trust import (
    ROLE_RELEASE,
    ROLE_ROOT,
    TrustedKey,
    TrustStore,
)
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.fake_channel import FakeChannelServer
from tests.fakes.t2_overlays import SYNTH2_RM_ID, SYNTH_RM_ID, make_overlay
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import FakeOsSlots, StubSession, bind_identity_to_sd
from tests.fakes.t7_bundles import fake_bit, sd_files
from tests.fakes.virtual_board import PRODUCT_V011_FEATURES, VirtualMps3, ila_v011_profile
from tests.spikes.harness_dist_publish import (
    MintRecord,
    ReleaseStore,
    build_release,
    channel_document,
    deterministic_zip,
    from_linux_bundle,
    publish_channel,
    tree_bytes,
)

V08 = ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"]
S_OLD, U_OLD = "0x3F1A560F", "0xD46FCDCB"         # fielded until 09-24
S_ILA, U_ILA = "0x72BB0A36", "0xC8551081"         # fielded 09-24 (v1.1.0 tag)
S_LNX, U_LNX = "0x4C1A0003", "0x3C0FFEE3"         # mint 3 (placeholder: not built yet)

RESULTS: list[tuple[str, str, str]] = []


def record(pid: str, verdict: str, detail: str) -> None:
    RESULTS.append((pid, verdict, detail))
    print(f"[{verdict:4}] {pid}: {detail}", flush=True)


def probe(pid: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def wrap(fn: Callable[..., None]) -> Callable[..., None]:
        def run(*a: Any, **kw: Any) -> None:
            t0 = time.monotonic()
            try:
                fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001 - a spike reports, it does not stop
                record(pid, "ERR", f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
            finally:
                print(f"       ({time.monotonic() - t0:.2f} s)", flush=True)
        return run
    return wrap


# --- fake mints ------------------------------------------------------------------------


def overlays(tmp: Path, static_id: str, usercode: str, names: tuple[str, ...],
             ip_class: str | None = None) -> dict[str, bytes]:
    root = tmp / f"ovl-{static_id}-{ip_class or 'open'}"
    ids = {"synth": SYNTH_RM_ID, "synth2": SYNTH2_RM_ID}
    for n in names:
        make_overlay(root, n, rm_id=ids[n], static_id=int(static_id, 16),
                     static_usercode=int(usercode, 16), ip_class=ip_class)
    return tree_bytes(root)


def bare_metal_mint(tmp: Path, static_id: str, usercode: str, fw_sha: str,
                    features: list[str], proto: str) -> MintRecord:
    bit = fake_bit(static_id=static_id, harness="1.0.0", sha=fw_sha, usercode=usercode,
                   features=features)
    return MintRecord(
        static_id=static_id, usercode=usercode, impl="bare-metal", fw_sha=fw_sha,
        wire_harness="1.0.0", ver32="0x01000000", proto=proto, features=features,
        vivado="2024.1", sd_files=sd_files(bit),
        overlays_open=overlays(tmp, static_id, usercode, ("synth",)),
        overlays_aaa=overlays(tmp, static_id, usercode, ("synth2",), ip_class="arm-aaa"),
        kit_zip=deterministic_zip({"kit.json": json.dumps({"static_id": static_id}).encode(),
                                   "static/static_routed_locked.dcp": b"\0" * 4096}))


def linux_mint(tmp: Path) -> MintRecord:
    # The shape tools/linux_bundle.py writes (FLOW_CONTRACT §0.1), trimmed to what is read.
    bundle = {"schema": "mps3-linux-bundle", "schema_version": 1, "mint_kind": "mint",
              "fieldable": True, "shell_cpu": "mbv", "static_id": S_LNX,
              "static_usercode": U_LNX, "static_ver32": "0x01000000",
              "targets": {"mcc_sd": {}, "ethernet": {"components": {
                  "harnessd_sha256": "5eed" + "0" * 60, "ver32": "0x01000000"}}}}
    bit = fake_bit(static_id=S_LNX, harness="1.0.0", sha="5eed0000", usercode=U_LNX,
                   features=[f for f in PRODUCT_V011_FEATURES if f != "windowed"])
    return from_linux_bundle(
        bundle, wire_harness="1.0.0", proto="0.14",
        features=[f for f in PRODUCT_V011_FEATURES if f != "windowed"], vivado="2026.1",
        sd_files=sd_files(bit), os_image=b"S0LB" + b"\x5a" * 65536,
        overlays_open=overlays(tmp, S_LNX, U_LNX, ("synth",)))


# --- the world -------------------------------------------------------------------------


class World:
    def __init__(self, ws: Path) -> None:
        self.ws = ws
        self.key = minisign.SecretKey.generate()          # throwaway, in memory only
        self.root_key = minisign.SecretKey.generate()
        self.trust = TrustStore(pinned=(
            TrustedKey(self.key.public, ROLE_RELEASE, ("stable", "beta", "dev"), "spike"),
            TrustedKey(self.root_key.public, ROLE_ROOT, (), "spike root")))
        self.mints = {
            "1.0.0": bare_metal_mint(ws / "m", S_OLD, U_OLD, "cb31b0f2", V08, "0.10"),
            "1.1.0": bare_metal_mint(ws / "m", S_ILA, U_ILA, "d68dd0ed",
                                     list(PRODUCT_V011_FEATURES), "0.11"),
            # a firmware-only re-bake (updatemem) on the SAME static: v0.12
            "1.1.1": bare_metal_mint(ws / "m", S_ILA, U_ILA, "0e12a0b0",
                                     list(PRODUCT_V011_FEATURES), "0.12"),
            "2.0.0": linux_mint(ws / "m"),
        }

    def publish(self, store: ReleaseStore, *, identity_harness: str | None = None,
                stable_serial: int = 1, beta_serial: int = 1,
                drop: tuple[str, ...] = ()) -> None:
        rels = {v: build_release(store, v, m, identity_harness=(
            v if identity_harness == "tag" else identity_harness)) for v, m in self.mints.items()}
        stable = [rels[v] for v in ("1.0.0", "1.1.0", "1.1.1") if v not in drop]
        publish_channel(store, channel_document("stable", stable_serial, self.key, stable,
                                                "1.1.1"), self.key)
        publish_channel(store, channel_document("beta", beta_serial, self.key,
                                                [*stable, rels["2.0.0"]], "2.0.0"), self.key)


class Board:
    """VirtualMps3 on the ILA static with firmware v0.11, Debug USB attached (local SD path)."""

    def __init__(self, ws: Path, trust: TrustStore, name: str) -> None:
        self.vb = VirtualMps3(ws / f"board-{name}", ila_v011_profile(int(S_ILA, 16)), usb=True)
        self.vb.__enter__()
        clock = FakeClock()
        self._saved = (mccmod.DEFAULT_CLOCK, mccmod.DEFAULT_SLEEP)
        mccmod.DEFAULT_CLOCK, mccmod.DEFAULT_SLEEP = clock, clock.sleep
        self.vb.mcc.clock = clock
        self.vb.mcc.down_s, self.vb.mcc.boot_s, self.vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        self.bound = bind_identity_to_sd(self.vb)
        self.eng = Engine(EngineConfig(state_dir=ws / f"state-{name}"),
                          packs={"mps3": Mps3Pack(console_ports=self.vb.console_ports)})
        self.session = self.eng.open(self.vb.candidate(usb=True), note="hdist spike")
        self.svc = UpdateService(self.eng, trust=trust, token="", app_version="0.1.0")

    def close(self) -> None:
        try:
            self.eng.close_all()
        finally:
            mccmod.DEFAULT_CLOCK, mccmod.DEFAULT_SLEEP = self._saved
            self.vb.__exit__(None, None, None)

    def ident(self) -> BoardIdentity:
        return self.session.identity()


def catalog_view(svc: UpdateService, session: Any, source: str, channel: str) -> list[dict]:
    """The missing "Harness versions" table: one plan per listed release (read-only)."""
    verified = svc.fetch_channel(channel, source)
    rows = []
    for rel in verified.channel.harness:
        plan, _ = svc.plan_harness(session, verified=verified, version=rel.version)
        rows.append({"version": rel.version, "status": rel.status, "static": rel.identity.static_id,
                     "impl": rel.identity.impl or "?", "mode": plan.mode, "rekey": plan.rekey,
                     "running": plan.running_release == rel.version,
                     "blockers": len(plan.blockers), "warnings": len(plan.warnings),
                     "why": (plan.blockers or [""])[0][:70]})
    return rows


# --- probes ----------------------------------------------------------------------------


@probe("P1 publish + list")
def p1(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    w.publish(store)
    before = {p.name: p.read_bytes() for p in (store.root / "assets").rglob("*") if p.is_file()}
    w.publish(ReleaseStore(store.root), stable_serial=2, beta_serial=2)   # a re-pack: same bytes
    same = all((store.root / "assets" / p.parent.name / n).read_bytes() == before[n]
               for n in before for p in [next((store.root / "assets").rglob(n))])
    rep = b.svc.check(channel="stable", source=srv.source())
    vers = [r["version"] for r in rep["releases"]["harness"]]
    size = sum(len(v) for v in before.values())
    record("P1 publish + list", "PASS" if vers == ["1.1.1", "1.1.0", "1.0.0"] and same else "FAIL",
           f"prototype publisher -> parse_channel -> minisign -> 127.0.0.1; T7 lists {vers}; "
           f"deterministic re-pack gives identical assets: {same}; {len(before)} assets, "
           f"{size / 1e3:.0f} kB (real: ~12.4 MB .bit per static, 24 MB slot image)")


@probe("P2 which release does the board run")
def p2(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    ident = b.ident()
    plan, _ = b.svc.plan_harness(b.session, channel="stable", source=srv.source())
    ok = plan.running_release == "1.1.0"
    record("P2 which release does the board run", "PASS" if ok else "GAP",
           f"board reports shell {ident.shell_id}, harness {ident.harness_version!r}, sha "
           f"{ident.firmware_sha}; T7 match_release says {plan.running_release or 'unrecorded'!r} "
           f"(truth: 1.1.0). 1.1.0 and 1.1.1 share static+usercode and the firmware says "
           f"'1.0.0' for both; match_release ignores fw_sha (planner.py:165-177)")


@probe("P3 catalogue view (per-version plans)")
def p3(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    rows = catalog_view(b.svc, b.session, srv.source(), "beta")
    for r in rows:
        print(f"       {r['version']:6} {r['status']:10} {r['static']} {r['impl']:10} "
              f"mode={r['mode']:8} rekey={str(r['rekey']):5} running={str(r['running']):5} "
              f"blockers={r['blockers']} {r['why']}")
    lnx = next(r for r in rows if r["version"] == "2.0.0")
    record("P3 catalogue view (per-version plans)", "NOTE",
           f"{len(rows)} rows from one plan per version: works as a thin layer over make_plan, "
           f"but it is not in T7 (service/API/UI plan only the current or one --version). "
           f"Linux 2.0.0 on this board: rekey={lnx['rekey']}, blockers={lnx['blockers']} "
           f"({lnx['why']!r})")


@probe("P4 install fw-only 1.1.1 (same static, local Debug USB)")
def p4(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    plan, verified = b.svc.plan_harness(b.session, channel="stable", source=srv.source())
    out = b.svc.install_harness(b.session, plan, plan.approve(), verified)
    ident = b.ident()
    record("P4 install fw-only 1.1.1 (same static, local Debug USB)",
           "PASS" if out.result == "installed" else "GAP",
           f"plan mode={plan.mode} rekey={plan.rekey}; result={out.result}; board now sha "
           f"{ident.firmware_sha}; overlays stored={out.stored}; skipped={list(out.skipped)}; "
           f"reboots={b.vb.reboots}")


@probe("P5 downgrade across statics to 1.0.0, then SD-backup rollback")
def p5(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    plan, verified = b.svc.plan_harness(b.session, channel="stable", source=srv.source(),
                                        version="1.0.0")
    try:
        plan.approve()
        no_phrase = "approved WITHOUT the phrase (bad)"
    except HarnessError as exc:
        no_phrase = f"refused without the phrase ({exc.message[:48]}...)"
    out = b.svc.install_harness(b.session, plan, plan.approve(consent=plan.consent_phrase),
                                verified)
    after = b.ident().shell_id
    rb = b.svc.rollback_harness(b.session)
    back = b.ident()
    ok = (out.result == "installed" and after.lower() == S_OLD.lower()
          and rb.result == "restored" and back.firmware_sha == "0e12a0b0")
    record("P5 downgrade across statics to 1.0.0, then SD-backup rollback",
           "PASS" if ok else "GAP",
           f"rekey={plan.rekey} phrase={plan.consent_phrase!r}, {no_phrase}; unusable={len(plan.unusable)} "
           f"items; install={out.result} (board {after}); rollback={rb.result} -> board "
           f"{back.shell_id} sha {back.firmware_sha} (1.1.1 again). The rollback rewrote the whole "
           f"SD from the backup zip: another ~12 MB write on the real board")


@probe("P6 release version on the wire (tag in identity.harness)")
def p6(w: World, ws: Path) -> None:
    root = ws / "www-tag"
    with FakeChannelServer(root) as srv:
        store = ReleaseStore(root)
        w.publish(store, identity_harness="tag")
        b = Board(ws, w.trust, "tag")
        try:
            plan, verified = b.svc.plan_harness(b.session, channel="stable", source=srv.source())
            out = b.svc.install_harness(b.session, plan, plan.approve(), verified)
        finally:
            b.close()
    record("P6 release version on the wire (tag in identity.harness)",
           "GAP" if out.result != "installed" else "PASS",
           f"publisher puts the TAG (1.1.1) in identity.harness; the firmware reports '1.0.0' "
           f"(VERSION is still 1.0.0 on the fielded lineage): running_release="
           f"{plan.running_release or 'unrecorded'!r}, install -> {out.result}. The SD was written "
           f"and the board booted it, but T7 can never confirm it")


@probe("P7 DUT kit as a channel component (KIT-STORE rm-kit)")
def p7(w: World, ws: Path) -> None:
    from harness_manager.services.update.kits import ChannelKits
    from harness_manager.services.update.planner import BoardView, make_plan

    store = ReleaseStore(ws / "www-kit")
    rel = build_release(store, "1.1.1", w.mints["1.1.1"], with_kit=True)
    try:
        publish_channel(store, channel_document("stable", 1, w.key, [rel], "1.1.1"), w.key)
        state = UpdateState.under(ws / "state-kit")
        client = ChannelClient(state, Downloader(state.cache), w.trust)
        kits = ChannelKits(client, client.downloader,
                           source=str(store.root) + "/channel/{channel}/channel.json")
        found = kits.list(S_ILA)
        blob = kits.fetch(S_ILA)
        ch = kits.verified()[0].channel
        ident = BoardIdentity(board_type="mps3", shell_id=S_OLD.lower(), harness_version="1.0.0")
        plan = make_plan(ch, BoardView(board_id="b", pack="mps3", identity=ident,
                                       has_storage=True, has_controller=True),
                         app_version="0.1.0")
        ok = len(found) == 1 and blob.read_bytes() == w.mints["1.1.1"].kit_zip and \
            "kit" not in plan.components
        record("P7 DUT kit as a channel component (KIT-STORE rm-kit)", "PASS" if ok else "GAP",
               f"accepted; kits.list({S_ILA}) -> {[(k.release, k.vivado) for k in found]}; "
               f"fetched {blob.stat().st_size} B sha-checked; the harness plan fetches "
               f"{plan.components} (never the kit)")
    except HarnessError as exc:
        record("P7 DUT kit as a channel component (KIT-STORE rm-kit)", "GAP",
               f"schema refuses it: {exc.message[:110]} (schema.py:53-65). The publisher's "
               "dogfood check caught it before signing")


@probe("P8 two board types on one host (serial store)")
def p8(w: World, ws: Path) -> None:
    root = ws / "www-boards"
    state = UpdateState.under(ws / "state-boards")
    client = ChannelClient(state, Downloader(state.cache), w.trust)
    for pack, serial in (("mps3", 5), ("kr260", 2)):
        store = ReleaseStore(root / pack)
        rel = build_release(store, "1.0.0", w.mints["1.0.0"])
        doc = channel_document("stable", serial, w.key, [rel], "1.0.0",
                               board={"pack": pack, "part": "x", "revisions": []})
        publish_channel(store, doc, w.key)
    tpl = str(root) + "/{pack}/channel/{channel}/channel.json"
    client.fetch("stable", tpl.replace("{pack}", "mps3"))
    try:
        client.fetch("stable", tpl.replace("{pack}", "kr260"))
        record("P8 two board types on one host (serial store)", "PASS", "both accepted")
    except HarnessError as exc:
        record("P8 two board types on one host (serial store)", "GAP",
               f"mps3 'stable' #5 accepted, then kr260 'stable' #2 refused: {exc.message[:90]}. "
               "SerialStore is keyed by channel name only (state.py:107-140)")
    for name in ("testing", "mps3-stable"):
        store = ReleaseStore(root / name)
        rel = build_release(store, "1.0.0", w.mints["1.0.0"])
        publish_channel(store, channel_document(name, 1, w.key, [rel], "1.0.0"), w.key)
        try:
            client.fetch(name, tpl.replace("{pack}", name))
            record(f"P8b channel name {name!r}", "PASS", "accepted")
        except HarnessError as exc:
            record(f"P8b channel name {name!r}", "NOTE",
                   f"refused: {exc.message[:80]} (trust.py:42-49: keys sign stable/beta/dev only)")


@probe("P9 private channel index (token)")
def p9(w: World, ws: Path) -> None:
    root = ws / "www-private"
    with FakeChannelServer(root) as srv:
        store = ReleaseStore(root / "private")          # the fake serves private/ only with a token
        rel = build_release(store, "1.0.0", w.mints["1.0.0"])
        publish_channel(store, channel_document("stable", 1, w.key, [rel], "1.0.0"), w.key)
        state = UpdateState.under(ws / "state-private")
        # The fake plays GitHub's API host: 127.0.0.1 is a token host here, as in the tests.
        twin = ChannelClient(UpdateState.under(ws / "state-private-twin"),
                             Downloader(ws / "c-twin", token=srv.token), w.trust)
        try:
            twin.fetch("stable", srv.base + "private/channel/{channel}/channel.json")
            twin_note = "a NON-token host got it (bad)"
        except HarnessError:
            twin_note = ("twin: with GitHub-only token hosts, 127.0.0.1 got no token "
                         f"(Authorization sent: {[r['auth'] for r in srv.requests]})")
        srv.requests.clear()
        dl = Downloader(state.cache, token=srv.token, token_hosts=frozenset({"127.0.0.1"}))
        client = ChannelClient(state, dl, w.trust)
        try:
            v = client.fetch("stable", srv.base + "private/channel/{channel}/channel.json")
            sent = [r["auth_value_ok"] for r in srv.requests]
            record("P9 private channel index (token)", "PASS" if all(sent) else "GAP",
                   f"serial {v.channel.serial} fetched with the token on both index requests "
                   f"({sent}); {twin_note}")
        except HarnessError as exc:
            sent = [r.get("auth") for r in srv.requests]
            record("P9 private channel index (token)", "GAP",
                   f"{exc.message[:70]}; Authorization sent on the index requests: {sent}. "
                   "fetch_bytes never sends the token (download.py:108-146), so a private "
                   "dist repo cannot host channel.json")


@probe("P10 offline mirror of absolute GitHub URLs")
def p10(w: World, ws: Path) -> None:
    from harness_manager.services.update.mirror import write_mirror

    web = ws / "www-origin"
    mirror = ws / "mirror"
    with FakeChannelServer(web) as origin:
        store = ReleaseStore(web, url_base=origin.base)    # absolute URLs, like GitHub Releases
        rel = build_release(store, "1.1.1", w.mints["1.1.1"])
        publish_channel(store, channel_document("stable", 1, w.key, [rel], "1.1.1"), w.key)
        # `harness mirror --to DIR` (H5): the exact signed channel + blobs/<sha256>
        state = UpdateState.under(ws / "state-mirror-writer")
        writer = ChannelClient(state, Downloader(state.cache), w.trust)
        report = write_mirror(writer.fetch("stable", origin.source()), writer.downloader, mirror)
    shutil.copytree(web, ws / "plain-copy")                # P10b: the old plain copy
    b = Board(ws, w.trust, "mirror")                        # the origin is gone now
    try:
        plan, verified = b.svc.plan_harness(b.session, channel="stable", source=str(mirror))
        try:
            out = b.svc.install_harness(b.session, plan, plan.approve(), verified)
            record("P10 offline mirror of absolute GitHub URLs",
                   "PASS" if out.result == "installed" else "GAP",
                   f"write_mirror -> {len(report.blobs)} blobs, skipped {list(report.skipped)}; "
                   f"origin down; --source {mirror.name}/ -> {out.result} from blobs/<sha256> "
                   "(no URL rewritten)")
        except HarnessError as exc:
            record("P10 offline mirror of absolute GitHub URLs", "GAP",
                   f"channel.json verified from the mirror, but: {exc.message[:120]}")
    finally:
        b.close()
    b = Board(ws, w.trust, "plain-copy")
    try:
        plan, verified = b.svc.plan_harness(
            b.session, channel="stable",
            source=str(ws / "plain-copy") + "/channel/{channel}/channel.json")
        try:
            b.svc.install_harness(b.session, plan, plan.approve(), verified)
            record("P10b a plain copy (no blobs/)", "NOTE", "installed (unexpected)")
        except HarnessError as exc:
            record("P10b a plain copy (no blobs/)", "NOTE",
                   f"still goes to the dead origin, as it should: {exc.message[:70]}. A mirror "
                   "is written by write_mirror / the release tool's --mirror")
    finally:
        b.close()


@probe("P11 retention: roll back to a release the channel no longer lists")
def p11(w: World, srv: FakeChannelServer, store: ReleaseStore, b: Board) -> None:
    w.publish(store, stable_serial=10, beta_serial=10, drop=("1.0.0",))
    plan, _ = b.svc.plan_harness(b.session, channel="stable", source=srv.source(), version="1.0.0")
    record("P11 retention: roll back to a release the channel no longer lists", "NOTE",
           f"blockers={plan.blockers}. Needs a retention rule (keep every release whose static "
           "is still fielded anywhere) or an 'archive' channel")


@probe("P12 Linux: OS-slot-only update, Ethernet only")
def p12(w: World, ws: Path, srv: FakeChannelServer) -> None:
    slots = FakeOsSlots()
    ident = BoardIdentity(board_type="mps3", shell_id=S_LNX.lower(), harness_version="1.0.0",
                          firmware_sha="5eed0000", harness_impl="linux", usercode=U_LNX.lower())
    sess = StubSession(ident, os_slots=slots)
    state = ws / "state-linux"
    svc = UpdateService(state_dir=state, trust=w.trust, token="", app_version="0.1.0",
                        store=None, os_slots_for=lambda s: s.os_slots)
    plan, _ = svc.plan_harness(sess, channel="beta", source=srv.source(), version="2.0.0")
    record("P12 Linux: OS-slot-only update, Ethernet only", "PASS" if plan.os_slot and not plan.base
           else "NOTE",
           f"mode={plan.mode} os_slot={plan.os_slot} base={plan.base} blockers={plan.blockers}; "
           "the adapter behind it (6910 kind-2 push + `slot` verb, SLOT_VERB_DRAFT) is T7-2, "
           "not built")


@probe("P14 Ethernet-only bare-metal board (hub, or standalone)")
def p14(w: World, srv: FakeChannelServer) -> None:
    ident = BoardIdentity(board_type="mps3", shell_id=S_ILA.lower(), harness_version="1.0.0",
                          firmware_sha="d68dd0ed", features=tuple(PRODUCT_V011_FEATURES))
    svc = UpdateService(state_dir=w.ws / "state-eth", trust=w.trust, token="", app_version="0.1.0",
                        store=None)
    base, _ = svc.plan_harness(StubSession(ident), channel="stable", source=srv.source())
    ovl, _ = svc.plan_harness(StubSession(ident), channel="stable", source=srv.source(),
                              overlays_only=True)
    record("P14 Ethernet-only bare-metal board (hub, or standalone)", "NOTE",
           f"base update blockers={base.blockers}; overlays-only mode={ovl.mode} "
           f"blockers={ovl.blockers}. No hub-side SD adapter exists: path (b) is blocked "
           "in HM although fpgahub can write nanosoc.bit (--method sd)")


@probe("P13 cache after the run")
def p13(ws: Path) -> None:
    blobs = [p for p in ws.glob("state-*/update/cache/blobs/*") if p.is_file()]
    backups = [p for p in ws.glob("state-*/update/backups/*/*") if p.is_file()]
    record("P13 cache after the run", "NOTE",
           f"{len(blobs)} cached blobs, {sum(p.stat().st_size for p in blobs) / 1e3:.0f} kB; "
           f"{len(backups)} backup files. No eviction or pinning policy exists (download.py); "
           "real sizes make it ~15 MB per bare-metal release, ~40 MB per Linux one")


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="hdist-", dir="/tmp"))
    print(f"workspace {ws}")
    t0 = time.monotonic()
    try:
        w = World(ws)
        with FakeChannelServer(ws / "www") as srv:
            store = ReleaseStore(ws / "www")
            b = Board(ws, w.trust, "main")
            try:
                p1(w, srv, store, b)
                p2(w, srv, store, b)
                p3(w, srv, store, b)
                p4(w, srv, store, b)
                p5(w, srv, store, b)
                p12(w, ws, srv)
                p14(w, srv)
                p11(w, srv, store, b)
            finally:
                b.close()
        p6(w, ws)
        p7(w, ws)
        p8(w, ws)
        p9(w, ws)
        p10(w, ws)
        p13(ws)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print(f"\n{len(RESULTS)} probes in {time.monotonic() - t0:.1f} s; workspace removed")
    for pid, verdict, _ in RESULTS:
        print(f"  {verdict:4} {pid}")
    return 0 if not any(v == "ERR" for _, v, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
