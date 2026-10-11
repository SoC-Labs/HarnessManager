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
| sha256 | `6937ea39abff5624c0aa3bb138a8aa99238de7e8e98e5e10fbd72969ad360c1a` |
| Built from | `git@github.com:SoC-Labs/MPS3-NanoSoC-Verification-Platform-dev.git` |
| Platform commit | `61d78b03b887beb2acf3eaa12f12ee286f9b52b7` (2026-10-08T14:52:27+01:00) |
| Commit subject | VERSION 2.0.1: the image reports harness "2.0.1" |
| Last pyverify change at or before it | `8bceab659fe7e0600022421bf6e29eebce394729` |
| Source | `git archive <commit> host/pyverify` (committed files only) |
| Built with | `pip wheel --no-deps`, pip 26.2.1, Python 3.11.13, SOURCE_DATE_EPOCH=1791467547 |

Check it: `sha256sum vendor/mps3_pyverify-0.1.0-py3-none-any.whl` must print the sha256 above.
`tests/unit/test_l5_release.py` checks this in `make check`.

## The MPS3 OpenOCD configs (openocd/)

`openocd/` (shipped in the wheel as `src/harness_manager_mps3/openocd_cfg/`) is `host/openocd` from the same platform commit (last changed at
`426fc91cb964ee3864c13e17fc17be1a6b82e6e7`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
`HARNESS_MANAGER_MPS3_OPENOCD_DIR` still overrides both.

| File | sha256 |
|---|---|
| `openocd/README.md` | `4ba7e4ecd9dc6a11b629cf62057af767be6cae540b75304b680e978c18983dc4` |
| `openocd/nanosoc_iice_chain.cfg` | `5932ad1f8adf046f1aa668ef1af825cf00e24c4e60cf70baa494ef52aa2d688f` |
| `openocd/nanosoc_mps3_jtag.cfg` | `c90b14130d96457b77f478b08271325575f78b88fcf12e2f0e1ddaa264477ad2` |
| `openocd/nanosoc_mps3_multicore_jtag.cfg` | `9c987d432d7f2d16f5e0a4b1c9f0b097932cb4f5f6364e4060148284632ec5b3` |
| `openocd/nanosoc_ops.tcl` | `37e8fae8d2c3c0ac024c178d9f7e7080a43c80cdd397a26c308866787cb2fb74` |
| `openocd/swd_cortex_m.cfg` | `a212d56a3c390e937212113634e1b67ed3e9ed9ae3a353e46e1b758d915f09c0` |
| `openocd/swd_remote_bitbang.cfg` | `cb84665c01a493a573bf52f308c187e2a67449ae8ffee3bcdf891446b8341ce1` |
