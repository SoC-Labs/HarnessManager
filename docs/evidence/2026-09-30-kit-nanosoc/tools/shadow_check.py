"""KIT-NANOSOC: is an imported overlay SHADOWED by an overlay directory holding the same
(rm_name, rm_id, static_id)? Board-free: the catalogue the Program page and `program` use,
built over (1) a COPY of the fielded nanosoc triple as an overlay dir (what --overlay-dir or
mps3.overlay_dirs gives) and (2) the private store holding this lane's `kit pack --import`.
Usage: python shadow_check.py OVERLAY_ROOT
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

from harness_manager.engine import resolve_state_dir
from harness_manager.services.store import ContentStore
from harness_manager_mps3.overlays import STORE_KIND, OverlayCatalogue

logging.basicConfig(level=logging.INFO, format="log %(levelname)s %(name)s: %(message)s")
root = Path(sys.argv[1])
store = ContentStore(resolve_state_dir() / "store")
print("store records:", [(m.get("rm_name"), m.get("rm_id")) for _s, m in store.find(STORE_KIND)])
for label, dirs in (("store only", []), ("overlay dir first, then the store", [root])):
    cat = OverlayCatalogue(dirs=dirs, use_env=False)
    cat.use_store(store)
    es = cat.entries()
    print(f"\n{label}: {len(es)} entr{'y' if len(es) == 1 else 'ies'}")
    for e in es:
        sha = hashlib.sha256(e.overlay.partial_path().read_bytes()).hexdigest()
        print(f"  {e.ref.name} {e.ref.rm_id} static {e.ref.static_id} origin {e.origin} "
              f"source {e.ref.source} partial {sha[:16]}")
