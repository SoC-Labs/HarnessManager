# Vendored from the platform repo

pyverify is the only codec for the MPS3 shell protocol. It lives in the platform
repo (mps3-nanosoc-platform, `host/pyverify`) and is not on PyPI. Harness Manager
needs it at run time, so its wheel is kept here. The installer and CI install it
by path first, then Harness Manager with `pip --find-links vendor`.

Do not edit anything here by hand. Rebuild it with `scripts/vendor_pyverify.sh` (or
`make vendor-pyverify`), which rewrites this file.

| Field | Value |
|---|---|
| Wheel | `mps3_pyverify-0.1.0-py3-none-any.whl` |
| sha256 | `ac1865a3dc7dd5c5be81f859bc54a7776ef5408b019fde31845bf9b46c48c9c1` |
| Built from | `git@github.com:SoC-Labs/MPS3-NanoSoC-Verification-Platform-Dev.git` |
| Platform commit | `df549e93751c13ba486b9dbf5c29722b5e674d2e` (2026-10-16T23:04:34+01:00) |
| Commit subject | soak_linux: swaps on a claimed board ride the ssh tunnel (6900 + 6910) |
| Last pyverify change at or before it | `35f9e43b19d0b068a251bcd4b87602b2f7706b97` |
| Source | `git archive <commit> host/pyverify` (committed files only) |
| Built with | `pip wheel --no-deps`, pip 26.2.1, Python 3.11.13, SOURCE_DATE_EPOCH=1792188274 |

Check it: `sha256sum vendor/mps3_pyverify-0.1.0-py3-none-any.whl` must print the sha256 above.
`tests/unit/test_l5_release.py` checks this in `make check`.

## The MPS3 OpenOCD configs (openocd/)

`openocd/` (shipped in the wheel as `src/harness_manager_mps3/openocd_cfg/`) is `host/openocd` from the same platform commit (last changed at
`9514a38b00e5125f01dfdec3192ea350b56bbb6f`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
`HARNESS_MANAGER_MPS3_OPENOCD_DIR` still overrides both.

| File | sha256 |
|---|---|
| `openocd/README.md` | `86ff39d65da59c4acb0d22d6a2653e824dd03321e05ba38c9bf836a387b26c3b` |
| `openocd/nanosoc_iice_chain.cfg` | `104617b30bd026bfe60eeaf867d42cd7af279484c5d521568e3c68995c8c6e4f` |
| `openocd/nanosoc_mps3_jtag.cfg` | `9d84609ed3e01c44f4ab0366a99f0f7d325c4749d0a9284a9088cfbd3e32a67c` |
| `openocd/nanosoc_mps3_multicore_jtag.cfg` | `b40153b4795a60a690fb1b6298abd635dc48fbd2aaa811660836e5604382ca71` |
| `openocd/nanosoc_ops.tcl` | `37e8fae8d2c3c0ac024c178d9f7e7080a43c80cdd397a26c308866787cb2fb74` |
| `openocd/nanosoc_qs_mps3_jtag.cfg` | `6e896147b0818a90dc3164ea0aee71f5277121e5e883e3570531b2ad25abb52b` |
| `openocd/nanosoc_qs_swd.cfg` | `5bdae9b19c229d93fb36dfa86e49659158e2fb679808fa335f9ad74edf5a6ca5` |
| `openocd/swd_cortex_m.cfg` | `a212d56a3c390e937212113634e1b67ed3e9ed9ae3a353e46e1b758d915f09c0` |
| `openocd/swd_over_jtagbb_proxy.py` | `3b573e9f6cd2eff7e57d57e7f2a9479d1c6f24f61af8f6780737164fcbccc106` |
| `openocd/swd_remote_bitbang.cfg` | `cb84665c01a493a573bf52f308c187e2a67449ae8ffee3bcdf891446b8341ce1` |
