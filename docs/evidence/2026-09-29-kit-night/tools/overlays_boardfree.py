"""Board-free "is it Programmable for static X?" over the PRIVATE content store.

`harness-manager overlays` needs a TARGET (a live shell). This runs the MPS3 deploy
preflight's own item functions against the catalogue built from the state dir's store,
with a SIMULATED live read (no socket, no board): shell_id = the static named on the
command line, impl linux, clr_max from the kit. The control/transport items need a board
and are reported as not run. usercode stays UNCHECKED (it needs JTAG).
Usage: python overlays_boardfree.py 0x44EE76D5 [clr_max]
"""
from __future__ import annotations

import sys

from harness_manager.engine import resolve_state_dir
from harness_manager.services.store import ContentStore
from harness_manager_mps3 import deploy as d
from harness_manager_mps3.overlays import OverlayCatalogue

sid = sys.argv[1]
clr_max = int(sys.argv[2]) if len(sys.argv) > 2 else None
state = resolve_state_dir()
cat = OverlayCatalogue(use_env=False)
cat.use_store(ContentStore(state / "store"))
live = d._Live(shell_id=sid, rm_id="0x00000000", version_ok=True, features=(), impl="linux",
               clr_max=clr_max)
print(f"state dir {state}; simulated live shell_id {sid} (NO board), clr_max {clr_max}")
print(f"catalogue: {len(cat.entries())} overlay(s); rejects: {cat.rejects or 'none'}")
bad_total = 0
for e in cat.entries():
    r = e.ref
    items = [d._check_shell_id(e, live), d._check_files(e),
             d.PreflightItem(d.ITEM_PAIR, e.pair_check, e.pair_detail),
             d._check_clearing_fits(e, live.clr_max), d._check_usercode(e, None)]
    bad = [i for i in items if i.check == d.Check.MISMATCH]
    bad_total += bool(bad)
    verdict = "PROGRAMMABLE (board-free items)" if not bad else "REFUSED"
    print(f"\n{r.name}  rm_id {r.rm_id}  static {r.static_id}  origin {e.origin}  "
          f"size {r.size_bytes}  ip_class {r.ip_class}  -> {verdict}")
    for i in items:
        print(f"  {i.check.value if hasattr(i.check, 'value') else i.check:<10} {i.name:<14} "
              f"{i.detail}")
    try:
        e.overlay.validate(expected_static_id=int(sid, 0))
        print(f"  ok         pyverify       Overlay.validate(expected_static_id={sid}) passed")
    except Exception as exc:  # noqa: BLE001
        bad_total += 1
        print(f"  MISMATCH   pyverify       Overlay.validate: {exc}")
    print("  (not run)  control/transport need a live shell")
sys.exit(1 if bad_total else 0)
