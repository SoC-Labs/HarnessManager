"""DUT build kits and the build guide (lanes KIT-STORE K1-K3/K5 and KIT-GUIDE KG-A..C).

- ``schema``: ``kit.json`` v1 and the build receipt v1;
- ``service``: ``KitService``: the kit cache (the engine's content store), its sources
  (cache, channel seam, hub archive path, a path), import, export and verify;
- ``vivado``: find the local Vivado and compare releases (HM warns; the script refuses);
- ``render``: ``build_rm.tcl`` from the package's one template (david K5);
- ``script``: ``kit script``, one build directory for a design;
- ``build``: the receipt read back, before it becomes an overlay;
- ``guide``: the six steps and their states.

The board-specific half is the pack's ``KitAdapter`` (``harness_manager_mps3.kit``).
docs/design/DUT_BUILD_KIT_STORAGE.md and DUT_BUILD_GUIDE.md are the designs.
"""

from __future__ import annotations

from .schema import (
    KIT_JSON,
    KIT_SCHEMA,
    BuildReceipt,
    KitFormatError,
    KitManifest,
    load_kit_json,
    load_receipt,
    parse_kit,
)
from .service import CachedKit, ChannelSource, HubSource, KitService, kit_adapter

__all__ = [
    "KIT_JSON", "KIT_SCHEMA", "BuildReceipt", "CachedKit", "ChannelSource", "HubSource",
    "KitFormatError", "KitManifest", "KitService", "kit_adapter", "load_kit_json",
    "load_receipt", "parse_kit",
]
