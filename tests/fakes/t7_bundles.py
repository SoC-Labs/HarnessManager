"""T7 test artefacts: harness bundle parts built from the virtual board's own layout.

- ``fake_bit``: a syntactically real Xilinx ``.bit`` (header with part and USERID)
  whose configuration data carries the identity the board will report once it
  boots it (``T7FAKE`` + JSON; ``t7_board.bind_identity_to_sd`` reads it back);
- ``sd_zip``: the config-SD part with FakeSdVolume's tree (config.txt,
  MB/HBI0309C/board.txt, Nanosoc/nanosoc.txt, Nanosoc/nanosoc.bit);
- ``overlays_zip``: overlay triples (T2's ``make_overlay``) keyed to a shell;
- ``Release``: one harness release's identity plus a helper that publishes its
  components through the ``ChannelBuilder``.

The fielded identity comes from ``virtual_board.FIELDED_3F1A560F``.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from socharness.services.update.bitheader import build_bit
from tests.fakes.fake_channel import AssetFile, ChannelBuilder
from tests.fakes.t2_overlays import FIELDED_USERCODE, SYNTH2_RM_ID, SYNTH_RM_ID, make_overlay
from tests.fakes.virtual_board import FIELDED_3F1A560F, FIELDED_ILA_V011

FIELDED_STATIC = f"0x{FIELDED_3F1A560F.static_id:08x}"        # 0x3f1a560f
FIELDED_HARNESS = FIELDED_3F1A560F.harness_version              # 1.0.0
FIELDED_FEATURES = list(FIELDED_3F1A560F.features)
FIELDED_SHA = FIELDED_3F1A560F.harness_sha                      # cb31b0f2
USERCODE = f"0x{FIELDED_USERCODE:08x}"                           # 0xd46fcdcb
NEW_STATIC = "0x5a11c0de"                                        # a re-keyed shell (synthetic)
NEW_USERCODE = "0x0badf00d"
#: The ILA mint (lab board from 09-24): the real re-key the first harness update performs.
ILA_STATIC = f"0x{FIELDED_ILA_V011.static_id:08x}"              # 0x72bb0a36 (T12 profile)
ILA_SHA = FIELDED_ILA_V011.harness_sha
PART = "xcku115-flvb2104-2-e"
FAKE_MAGIC = b"T7FAKE"


def fake_bit(*, static_id: str, harness: str, sha: str = "c0ffee00", usercode: str | None,
             features: list[str] | None = None, part: str = PART,
             wire_usercode: str | None = None) -> bytes:
    """``wire_usercode``: what the booted firmware reports in ``version.usercode``
    (net-protocol v0.12); None = a firmware that does not report it (v0.10/v0.11)."""
    ident = {"static_id": static_id, "harness": harness, "sha": sha,
             "features": features if features is not None else FIELDED_FEATURES,
             "wire_usercode": wire_usercode}
    return build_bit("shell_wrapper", part, FAKE_MAGIC + json.dumps(ident).encode(),
                     userid=usercode)


def read_fake_identity(bit: bytes) -> dict[str, Any] | None:
    """The identity a fake .bit carries, or None for any other file."""
    from socharness.services.update.bitheader import BitHeaderError, parse_bit_header

    try:
        hdr = parse_bit_header(bit)
    except BitHeaderError:
        return None
    data = bit[hdr.data_offset:hdr.data_offset + hdr.data_len]
    if not data.startswith(FAKE_MAGIC):
        return None
    return json.loads(data[len(FAKE_MAGIC):])


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            zf.writestr(name, data)
    return buf.getvalue()


def sd_files(bit: bytes, *, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """The SD tree FakeSdVolume models, with a new bitstream."""
    files = {
        "config.txt": b"TITLE: V2M-MPS3 config\nUSB_REMOTE: TRUE\nUARTMODE: 0\n",
        "MB/HBI0309C/board.txt": b"APPFILE: Nanosoc\\nanosoc.txt\n",
        "MB/HBI0309C/Nanosoc/nanosoc.txt": b"F0FILE: nanosoc.bit\n[OSCCLKS]\nOSC0: 25.0\nOSC1: 50.0\n",
        "MB/HBI0309C/Nanosoc/nanosoc.bit": bit,
    }
    files.update(extra or {})
    return files


def overlays_zip(tmp: Path, *, static_id: str, usercode: str | None,
                 names: tuple[str, ...] = ("synth", "synth2"), ip_class: str | None = None,
                 corrupt: str = "") -> bytes:
    """A zip of overlay triples (``<rm>/{manifest.json,<rm>.bin,<rm>_clear.bin}``)."""
    root = tmp / f"ovl-{static_id}-{'-'.join(names)}-{corrupt or 'ok'}"
    ids = {"synth": SYNTH_RM_ID, "synth2": SYNTH2_RM_ID}
    files: dict[str, bytes] = {}
    for i, name in enumerate(names):
        d = make_overlay(root, name, rm_id=ids.get(name, SYNTH_RM_ID + 16 + i),
                         static_id=int(static_id, 16),
                         static_usercode=int(usercode, 16) if usercode else None,
                         ip_class=ip_class, corrupt_partial=(corrupt == name))
        for p in sorted(d.iterdir()):
            files[f"{name}/{p.name}"] = p.read_bytes()
    return zip_bytes(files)


@dataclass
class Release:
    """One harness release, as the tests publish it."""

    version: str
    static_id: str = FIELDED_STATIC
    usercode: str = USERCODE
    features: list[str] = field(default_factory=lambda: list(FIELDED_FEATURES))
    sha: str = "c0ffee00"
    bit_usercode: str | None = None       # override the .bit header's USERID (a lie)
    bit_part: str = PART
    sd_extra: dict[str, bytes] = field(default_factory=dict)
    overlay_static: str | None = None     # override the overlays' key (a mismatch)
    overlay_usercode: str | None = "same"
    corrupt_overlay: str = ""
    with_sd: bool = True
    with_overlays: bool = True
    private_overlays: bool = False
    impl: str = "bare-metal"
    wire_usercode: str | None = None      # the usercode the firmware reports (v0.12); None: silent

    def identity(self) -> dict[str, Any]:
        return {"static_id": self.static_id, "usercode": self.usercode, "harness": self.version,
                "impl": self.impl, "proto": "0.10", "features": list(self.features),
                "fw_sha": self.sha}

    def bit(self) -> bytes:
        return fake_bit(static_id=self.static_id, harness=self.version, sha=self.sha,
                        usercode=self.bit_usercode or self.usercode, features=self.features,
                        part=self.bit_part, wire_usercode=self.wire_usercode)

    def components(self, builder: ChannelBuilder, tmp: Path) -> list[dict[str, Any]]:
        comps = []
        if self.with_sd:
            data = zip_bytes(sd_files(self.bit(), extra=self.sd_extra))
            comps.append(builder.component(
                "sd-HBI0309C", "mcc-sd",
                AssetFile(f"mps3-harness-{self.version}-sd-HBI0309C.zip", data), kind="sd"))
        if self.with_overlays:
            ucode = self.usercode if self.overlay_usercode == "same" else self.overlay_usercode
            data = overlays_zip(tmp, static_id=self.overlay_static or self.static_id,
                                usercode=ucode, corrupt=self.corrupt_overlay)
            comps.append(builder.component(
                "overlays-open", "host-store",
                AssetFile(f"mps3-harness-{self.version}-overlays-open.zip", data), kind="overlays",
                ip_class="open"))
        if self.private_overlays:
            data = overlays_zip(tmp, static_id=self.static_id, usercode=self.usercode,
                                names=("synth2",), ip_class="arm-aaa")
            comps.append(builder.component(
                "overlays-aaa", "host-store",
                AssetFile(f"mps3-harness-{self.version}-overlays-aaa.zip", data, private=True),
                kind="overlays", ip_class="arm-aaa"))
        return comps

    @classmethod
    def fielded(cls, **kw: Any) -> Release:
        """What the virtual board runs today (FIELDED_3F1A560F), as a channel release."""
        return cls("1.0.0", sha=FIELDED_SHA, **kw)

    def add_to(self, builder: ChannelBuilder, tmp: Path, **kw: Any) -> dict[str, Any]:
        return builder.add_harness(self.version, self.identity(), self.components(builder, tmp),
                                   **kw)
