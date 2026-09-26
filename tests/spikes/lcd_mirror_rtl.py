"""LCD-MIRROR spike 3: is a static-shell 8080 bus snooper cheap and exact? Icarus says yes.

Board-free; needs ``iverilog``/``vvp`` (Icarus 12+) on PATH and nothing else. NOT a test
(``tests/spikes`` is never collected). Run by hand::

    python3 tests/spikes/lcd_mirror_rtl.py            # build, simulate, compare, print a verdict
    python3 tests/spikes/lcd_mirror_rtl.py --mutant byte-order   # must be caught (also:
                                                      # wrap-off-by-one, snap-no-clear)

It replays ONE continuous bus history through ``lcd_mirror_rtl/lcdm_snoop.sv`` (a sketch
of the snooper the design asks the FPGA lane for) with an 8080 bus-functional model
(``lcd_mirror_rtl/tb_lcdm_snoop.sv``), in the order a KVM session produces it:

    harness boot -> link-down repaint -> DUT-OSD banner -> [KVM reset] -> clcd_demo frame 5
    -> clcd_demo frame 6 -> [KVM reset] -> nanosoc ahb_clcd demo -> [KVM reset] -> harness regain

The harness parts use clcd_0's real strobe timing, the DUT parts a slower, uneven one. After
every stage the bench snapshots the dirty map. The same history goes through the Python
model (``lcd_mirror_decoder.Hx8347dShadow``) and the spike then requires, bit for bit:

* the RTL's physical GRAM == the model's GRAM (all 76,800 pixels);
* the RTL's register shadow == the model's, for every register the mirror interprets;
* each RTL dirty snapshot == the set of 16x16 tiles in which some write CHANGED a pixel during
  that stage (compare-on-write: a DUT repainting an unchanged picture dirties nothing);
* the counters (bytes, pixels, RAMWR, panel resets) == the model's.

It also prints the cost the sim can see (storage bits). LUT/FF counts need a synthesis run,
which is the FPGA lane's (never Vivado here).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lcd_mirror_decoder as m  # noqa: E402

RTL = HERE / "lcd_mirror_rtl"
INTERPRETED = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x16, 0x17, 0x1F, 0x28, 0x36]


def history():
    """[(label, class, records)]: class 0 = harness timing, 1 = DUT timing."""
    out = [("harness:boot", 0, m.load_stream("boot")),
           ("harness:link_down", 0, m.load_stream("link_down")),
           ("harness:banner", 0, m.load_stream("banner"))]
    if (m.PLATFORM / "tests" / "clcd_demo" / "card_model.py").exists():
        out += [("dut:clcd_demo#5", 1, m.ctrl_reset_pulse() + m.clcd_demo_frame(5)[0]),
                ("dut:clcd_demo#6", 1, m.clcd_demo_frame(6)[0]),
                ("dut:nanosoc", 1, m.ctrl_reset_pulse() + m.nanosoc_demo()[0])]
    out.append(("harness:regain", 0, m.ctrl_reset_pulse() + m.load_stream("regain")))
    return out


#: ``--mutant NAME``: a one-line fault in a temporary copy of the RTL. Each one must FAIL
#: the comparison (a bench that cannot fail proves nothing).
MUTANTS = {
    "byte-order": ("wr_px_q   <= {hi_q, pd_q};", "wr_px_q   <= {pd_q, hi_q};"),
    "wrap-off-by-one": ("if (col_q >= ec) begin", "if (col_q > ec) begin"),
    "snap-no-clear": ("        dirty_live <= {NTILES{1'b0}};\n      end else if (changed)",
                      "      end else if (changed)"),
}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mutant", choices=sorted(MUTANTS), help="inject one fault; the run must FAIL")
    args = ap.parse_args(argv)
    if not shutil.which("iverilog"):
        print("iverilog not on PATH: nothing to do")
        return 2
    work = Path(tempfile.mkdtemp(prefix="lcdmirror-rtl-", dir="/tmp"))
    try:
        # 1. the stimulus and the model's expectations, stage by stage
        model = m.Hx8347dShadow()
        stim, want_snaps, labels = [], [], []
        for label, cls, recs in history():
            model.changed.clear()
            model.feed(recs)
            want_snaps.append(set(model.changed))
            labels.append(label)
            stim += [f"{cls} {rs:x} {b:02x}" for rs, b in recs]
            stim.append(f"{cls} 3 00")
        (work / "stim.txt").write_text("\n".join(stim) + "\n")

        # 2. build + simulate
        t0 = time.perf_counter()
        rtl = (RTL / "lcdm_snoop.sv").read_text()
        if args.mutant:
            old, new = MUTANTS[args.mutant]
            assert rtl.count(old) == 1, f"mutant {args.mutant}: anchor not found once"
            rtl = rtl.replace(old, new)
        (work / "lcdm_snoop.sv").write_text(rtl)
        subprocess.run(["iverilog", "-g2012", "-o", str(work / "sim.vvp"),
                        str(work / "lcdm_snoop.sv"), str(RTL / "tb_lcdm_snoop.sv")], check=True)
        subprocess.run(["vvp", "-n", str(work / "sim.vvp"), f"+stim={work / 'stim.txt'}",
                        f"+out={work}"], check=True, stdout=subprocess.DEVNULL)
        sim_s = time.perf_counter() - t0

        # 3. compare
        fails = []
        def memh(name):
            return [int(ln, 16) for ln in (work / name).read_text().splitlines()
                    if ln.strip() and not ln.startswith("//")]
        gram = memh("gram.hex")
        bad = [i for i in range(m.GW * m.GH) if gram[i] != model.gram[i]]
        if bad:
            i = bad[0]
            fails.append(f"GRAM: {len(bad)} pixels differ, first at gx={i % m.GW} gy={i // m.GW}: "
                         f"rtl 0x{gram[i]:04X} model 0x{model.gram[i]:04X}")
        regs = memh("regs.hex")
        for r in INTERPRETED:
            if regs[r] != model.regs[r]:
                fails.append(f"REG 0x{r:02X}: rtl 0x{regs[r]:02X} model 0x{model.regs[r]:02X}")
        snaps = (work / "snaps.txt").read_text().split()
        for label, want, got_hex in zip(labels, want_snaps, snaps, strict=True):
            v = int(got_hex, 16)
            got = {t for t in range(300) if (v >> t) & 1}
            verdict = "==" if got == want else "!="
            print(f"  {label:20} dirty tiles rtl {len(got):3d} {verdict} model-changed {len(want):3d}")
            if got != want:
                fails.append(f"DIRTY {label}: rtl-only {sorted(got - want)[:5]} model-only {sorted(want - got)[:5]}")
        counters = (work / "counters.txt").read_text().split()
        cv = dict(zip(counters[0::2], counters[1::2], strict=True))
        n_bytes = sum(1 for _, _, recs in history() for rs, _ in recs if rs in (0, 1))
        if int(cv["bytes"]) != n_bytes:
            fails.append(f"bytes: rtl {cv['bytes']} stream {n_bytes}")
        if int(cv["pixels"]) != model.pixels:
            fails.append(f"pixels: rtl {cv['pixels']} model {model.pixels}")
        print(f"  counters: {' '.join(counters)}")
        print(f"  storage: GRAM {m.GW * m.GH * 16} bits = 38 x RAMB36 (2K x 18); "
              f"register shadow 2048 bits (LUTRAM); dirty map 2 x 300 flops")
        print(f"  simulated {int(cv['sim_ns']) / 1e6:.1f} ms of bus in {sim_s:.0f} s wall")
        for f in fails:
            print("  FAIL", f)
        if args.mutant:
            print(f"MUTANT {args.mutant}:", "caught (the bench FAILS, as it must)" if fails
                  else "NOT CAUGHT -- the bench is blind to it")
            return 0 if fails else 1
        print("RESULT:", "PASS" if not fails else f"FAIL ({len(fails)})")
        return 1 if fails else 0
    finally:
        if os.environ.get("LCDM_KEEP"):
            print("kept", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
