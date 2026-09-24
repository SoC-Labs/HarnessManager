"""HARNESS-CAT fixtures: a signed multi-version harness catalogue, and boards to list it for.

The catalogue is the HARNESS-DIST spike's (``tests/spikes/harness_dist_spike.py``): four
fake mints in the real formats (an SD tree whose ``nanosoc.bit`` has a real Xilinx header,
T2 overlay triples keyed to each static, an Arm-IP overlay set marked ``github-token``, a
Linux slot image), published with the spike's prototype release command
(``harness_dist_publish``: validated with the app's own parser, deterministic zips) and
signed with a THROWAWAY minisign key generated in memory. ``CatalogWorld.serve()`` puts it
on 127.0.0.1 (T7's ``FakeChannelServer``); ``file_source()`` is the same tree as a local
directory (a ``file://`` source, which also serves the Arm-IP part without a token).

- ``stable``: 1.0.0 @0x3F1A560F (fw cb31b0f2), 1.1.0 @0x72BB0A36 (fw d68dd0ed, what the
  fielded ILA board runs), 1.1.1 @0x72BB0A36 (fw re-bake 0e12a0b0), current 1.1.1;
- ``beta``: the same plus 2.0.0, Linux @0x4C1A0003 (a placeholder static), current 2.0.0.

``BoardRig`` is a VirtualMps3 on the ILA static with the v0.11 firmware (it reports
``harness 1.0.0``, fw d68dd0ed: release 1.1.0) and the Debug USB, driven through the real
MPS3 pack with the FakeMcc on a fake clock; its shell reports whatever the SD now holds
(``bind_identity_to_sd``). Nothing reaches beyond 127.0.0.1 or the test's tmp dir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update import UpdateService, minisign
from harness_manager.services.update.trust import ROLE_RELEASE, ROLE_ROOT, TrustedKey, TrustStore
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.fake_channel import FakeChannelServer
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import bind_identity_to_sd
from tests.fakes.virtual_board import PRODUCT_V011_FEATURES, VirtualMps3, ila_v011_profile
from tests.spikes.harness_dist_publish import (
    ReleaseStore,
    build_release,
    channel_document,
    publish_channel,
)
from tests.spikes.harness_dist_spike import (
    S_ILA,
    S_LNX,
    S_OLD,
    U_ILA,
    U_OLD,
    V08,
    bare_metal_mint,
    linux_mint,
)

__all__ = ["S_ILA", "S_LNX", "S_OLD", "U_ILA", "U_OLD", "BoardRig", "CatalogWorld", "NOTES"]

NOTES = {
    "1.0.0": "The fielded static until 09-24.",
    "1.1.0": "RM ILAs over XVC; the ILA static 0x72BB0A36.",
    "1.1.1": "Firmware re-bake: the console flush fix.",
    "2.0.0": "The MicroBlaze V Linux harness (mint 3).",
}
STABLE = ("1.0.0", "1.1.0", "1.1.1")


@dataclass
class CatalogWorld:
    """The fake mints, the throwaway keys and the published tree under ``root``."""

    root: Path
    key: minisign.SecretKey = field(default_factory=minisign.SecretKey.generate)
    root_key: minisign.SecretKey = field(default_factory=minisign.SecretKey.generate)
    mints: dict[str, Any] = field(default_factory=dict)
    serial: int = 0

    def __post_init__(self) -> None:
        m = self.root / "mints"
        self.mints = {
            "1.0.0": bare_metal_mint(m, S_OLD, U_OLD, "cb31b0f2", V08, "0.10"),
            "1.1.0": bare_metal_mint(m, S_ILA, U_ILA, "d68dd0ed", list(PRODUCT_V011_FEATURES),
                                     "0.11"),
            "1.1.1": bare_metal_mint(m, S_ILA, U_ILA, "0e12a0b0", list(PRODUCT_V011_FEATURES),
                                     "0.12"),
            "2.0.0": linux_mint(m),
        }
        self.store = ReleaseStore(self.www)

    @property
    def www(self) -> Path:
        return self.root / "www"

    def trust(self) -> TrustStore:
        return TrustStore(pinned=(
            TrustedKey(self.key.public, ROLE_RELEASE, ("stable", "beta", "dev"), "hcat throwaway"),
            TrustedKey(self.root_key.public, ROLE_ROOT, (), "hcat throwaway root")))

    def publish(self, *, stable: tuple[str, ...] = STABLE, stable_current: str = "1.1.1",
                beta_current: str = "2.0.0", withdrawn: tuple[str, ...] = (),
                beta: bool = True) -> None:
        """Sign and write both channels (the serial goes up on every publish)."""
        self.serial += 1
        rels = {}
        for v, mint in self.mints.items():
            entry = build_release(self.store, v, mint, notes_url=f"https://example.invalid/{v}")
            entry["notes"] = NOTES[v]
            if v in withdrawn:
                entry["status"] = "withdrawn"
            rels[v] = entry
        publish_channel(self.store, channel_document(
            "stable", self.serial, self.key, [rels[v] for v in stable], stable_current), self.key)
        if beta:
            publish_channel(self.store, channel_document(
                "beta", self.serial, self.key, [*(rels[v] for v in stable), rels["2.0.0"]],
                beta_current), self.key)

    def serve(self) -> FakeChannelServer:
        return FakeChannelServer(self.www)

    def file_source(self) -> str:
        return str(self.www / "channel" / "{channel}" / "channel.json")


class BoardRig:
    """A VirtualMps3 on the ILA static (release 1.1.0), Debug USB attached, on a fake clock,
    in an Engine whose update service trusts the world's throwaway key.

    ``monkeypatch`` moves the MCC module's clock (restored by pytest)."""

    def __init__(self, tmp: Path, world: CatalogWorld, monkeypatch: Any, *, name: str = "a",
                 usb: bool = True, stale: bool = False, state_dir: Path | None = None) -> None:
        self.vb = VirtualMps3(tmp / f"board-{name}", ila_v011_profile(int(S_ILA, 16)), usb=usb)
        self.vb.__enter__()
        clock = FakeClock()
        monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
        monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
        self.vb.mcc.clock = clock
        self.vb.mcc.down_s, self.vb.mcc.boot_s, self.vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        self.bound = bind_identity_to_sd(self.vb, stale=stale)
        self.state_dir = state_dir or tmp / "state"
        self.engine = Engine(EngineConfig(state_dir=self.state_dir),
                             packs={"mps3": Mps3Pack(console_ports=self.vb.console_ports)})
        self.svc = UpdateService(self.engine, trust=world.trust(), token="", app_version="0.1.0")
        self.engine._services["update"] = self.svc       # engine.update is lazy: seed it
        self.usb = usb
        self._session: Any = None

    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = self.engine.open(self.vb.candidate(usb=self.usb), note="hcat")
        return self._session

    def cli_args(self, source: str) -> list[str]:
        vb = self.vb
        usb = ["--serial", vb.mcc_url, "--volume", str(vb.sd.root)] if self.usb else []
        return [vb.shell_endpoint, *usb, "--source", source]

    def close(self) -> None:
        try:
            self.engine.close_all()
        finally:
            self.vb.__exit__(None, None, None)
