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
| sha256 | `8bf0902fc867e0c920feb23422ca47fe0ae7240024601783fa5f4e2072d4609f` |
| Built from | `git@github.com:SoC-Labs/MPS3-NanoSoC-Verification-Platform.git` |
| Platform commit | `3bfda65ce316c0fd09fc33f6bcef58dff65ea573` (2026-09-25T11:18:11+01:00) |
| Commit subject | B1 v4 fixes: swaps on a Linux harness go over TCP (the swap-away-from-a-DAP-RM race); B1 runner halts with nanosoc_halt_examine and gates the DUT console on stage0's markers; one-ssh mailbox read |
| Last pyverify change at or before it | `3bfda65ce316c0fd09fc33f6bcef58dff65ea573` |
| Source | `git archive <commit> host/pyverify` (committed files only) |
| Built with | `pip wheel --no-deps`, pip 26.2.1, Python 3.11.13, SOURCE_DATE_EPOCH=1790331491 |

Check it: `sha256sum vendor/mps3_pyverify-0.1.0-py3-none-any.whl` must print the sha256 above.
`tests/unit/test_l5_release.py` checks this in `make check`.

## The MPS3 OpenOCD configs (openocd/)

`openocd/` (shipped in the wheel as `src/harness_manager_mps3/openocd_cfg/`) is `host/openocd` from the same platform commit (last changed at
`ef31e25cb1d1a9f6ae7620398da500c926f03973`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
`HARNESS_MANAGER_MPS3_OPENOCD_DIR` still overrides both.

| File | sha256 |
|---|---|
| `openocd/README.md` | `dd8a2238f03c1f936b2e0420a4d6907b004029969417a4850210e8a15694ec83` |
| `openocd/nanosoc_iice_chain.cfg` | `5932ad1f8adf046f1aa668ef1af825cf00e24c4e60cf70baa494ef52aa2d688f` |
| `openocd/nanosoc_mps3_jtag.cfg` | `1b9131bfd5bc60c532ba0cfd95b3bfd74a8838b3d74abca4b710165d0e9e53a2` |
| `openocd/nanosoc_ops.tcl` | `37e8fae8d2c3c0ac024c178d9f7e7080a43c80cdd397a26c308866787cb2fb74` |
| `openocd/swd_cortex_m.cfg` | `a212d56a3c390e937212113634e1b67ed3e9ed9ae3a353e46e1b758d915f09c0` |
| `openocd/swd_remote_bitbang.cfg` | `cb84665c01a493a573bf52f308c187e2a67449ae8ffee3bcdf891446b8341ce1` |
