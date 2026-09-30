"""Write designs/nanosoc/nanosoc.json: the platform's nanosoc RM as an HM design document.
Sources = nanosoc_m0_soc/pynq/filelist.tcl expanded in order (flist_expanded.txt), then the
platform wrapper set in ooc_synth.tcl's order. Arm IP stays in place (absolute, read-only);
the SoC + ahb_qspi + platform files are the scratch snapshot (relative to the design file)."""
import json, pathlib
D = pathlib.Path("/tmpdir/claude-74755/kit-nanosoc/home/designs/nanosoc")
ROOTS = {"/home/dam1n19/SoCLabs/nanosoc_m0_soc/": "src/nanosoc_m0_soc/",
         "/home/dam1n19/SoCLabs/ahb_qspi/": "src/ahb_qspi/"}
def rel(p):
    for a, b in ROOTS.items():
        if p.startswith(a):
            return b + p[len(a):]
    assert p.startswith("/research/AAA/ip_library/"), p
    return p
srcs, incs, defs = [], [], []
for line in open("flist_expanded.txt"):
    w = line.split()
    if w[0] == "SRC":
        srcs.append(rel(w[2]))
    elif w[0] == "PROP" and w[1] == "include_dirs":
        incs = [rel(x) for x in w[2:]]
    elif w[0] == "PROP" and w[1] == "verilog_define":
        defs = w[2:]
plat = "src/platform/fpga/"
srcs += [plat + "rp/nanosoc/uart_axis_shim.sv", plat + "rp/nanosoc/rp_nanosoc_wrapper.sv",
         plat + "shell/ip/clcd/clcd_core.sv", plat + "rp_nanosoc_exp_placeholder"]
srcs[-1:] = [plat + "rp/nanosoc_exp/ahb_clcd.sv", plat + "rp/nanosoc_exp/nanosoc_exp_socket.sv"]
for s in srcs:
    assert (D / s).is_file() if not s.startswith("/") else pathlib.Path(s).is_file(), s
doc = {
    "kind": "rm",
    "name": "nanosoc",
    "title": "single-core nanoSoC (Cortex-M0) RM, the platform's rm_nanosoc, built by HM's kit flow (KIT-NANOSOC)",
    "rm_id": "0x01000001",
    "use": {"clkrst": {}, "jtag": {}, "uart": {}, "status": {}, "gpio": {}, "qspi": {"timed": True}},
    "clocks": ["jtag_tck"],
    "wrapper": plat + "rp/nanosoc/rp_nanosoc_wrapper.sv",
    "build": {
        "top": "rp_nanosoc_wrapper",
        "sources": srcs,
        "include_dirs": incs,
        "defines": defs,
        "synth_hook": "nanosoc_synth_hook.tcl",
    },
}
(D / "nanosoc.json").write_text(json.dumps(doc, indent=1) + "\n")
print(len(srcs), "sources,", len(incs), "include dirs, defines", defs)
