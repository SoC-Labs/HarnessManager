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
| sha256 | `124c737ef7c649fdb7b2eeeb831f05d1d73072f5333ebcca384e59852e7c6b78` |
| Built from | `git@github.com:SoC-Labs/MPS3-NanoSoC-Verification-Platform.git` |
| Platform commit | `b2b83d38448ec1ee2decef938ebed1f25537d9c5` (2026-09-23T14:28:47+01:00) |
| Commit subject | docs: RM-internal ILA over XVC handover + B1 XVC smoke evidence (PASS) |
| Last pyverify change at or before it | `ccc2fdeee2f7c88dbdfbbffd6f2ee1d82d8751f4` |
| Source | `git archive <commit> host/pyverify` (committed files only) |
| Built with | `pip wheel --no-deps`, pip 26.2.1, Python 3.11.13, SOURCE_DATE_EPOCH=1790170127 |

Check it: `sha256sum vendor/mps3_pyverify-0.1.0-py3-none-any.whl` must print the sha256 above.
`tests/unit/test_l5_release.py` checks this in `make check`.

## The MPS3 OpenOCD configs (openocd/)

`openocd/` (shipped in the wheel as `src/harness_manager_mps3/openocd_cfg/`) is `host/openocd` from the same platform commit (last changed at
`cb45c189c6424451dd24779f7656fbdfc3b03f90`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
`HARNESS_MANAGER_MPS3_OPENOCD_DIR` still overrides both.

| File | sha256 |
|---|---|
| `openocd/README.md` | `f3d33987e0a05f5c836da56f29d87f912c69b0e970a52d9c3a91b138283127ab` |
| `openocd/nanosoc_iice_chain.cfg` | `5932ad1f8adf046f1aa668ef1af825cf00e24c4e60cf70baa494ef52aa2d688f` |
| `openocd/nanosoc_mps3_jtag.cfg` | `1b9131bfd5bc60c532ba0cfd95b3bfd74a8838b3d74abca4b710165d0e9e53a2` |
| `openocd/nanosoc_ops.tcl` | `37e8fae8d2c3c0ac024c178d9f7e7080a43c80cdd397a26c308866787cb2fb74` |
| `openocd/swd_cortex_m.cfg` | `a212d56a3c390e937212113634e1b67ed3e9ed9ae3a353e46e1b758d915f09c0` |
| `openocd/swd_remote_bitbang.cfg` | `cb84665c01a493a573bf52f308c187e2a67449ae8ffee3bcdf891446b8341ce1` |
