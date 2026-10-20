# Vendored from the platform repo (a fallback that goes away)

**This wheel is a dev and CI fallback only, and it goes away once the platform release is
published.** Harness Manager pins the platform's PUBLISHED wheel by exact version and sha256
(`pyverify.pin`, set with `scripts/pin_pyverify.sh <version> <sha256>`). The installers and
`make_wheelhouse.sh` use a wheel from this directory only when its sha256 equals the pin's;
otherwise they download the pinned URL and verify it. When v<version> is published and pinned:
`git rm vendor/mps3_pyverify-*.whl` (keep the OpenOCD table below, it describes
`src/harness_manager_mps3/openocd_cfg/`).

pyverify is the only codec for the MPS3 shell protocol. It lives in the platform
repo (mps3-nanosoc-platform, `host/pyverify`) and is not on PyPI.

Do not edit anything here by hand. Rebuild it with `scripts/vendor_pyverify.sh` (or
`make vendor-pyverify`), which rewrites this file.

| Field | Value |
|---|---|
| Wheel | `mps3_pyverify-2.2.1-py3-none-any.whl` |
| sha256 | `0347a314a10b6202b975f7a1e97dcf6207dede81013477d66690ec8d11ba06d5` |
| Built from | `git@github.com:SoC-Labs/nanoSoC-Hardware-Prototyping-Platform-Dev.git` |
| Platform commit | `fd64d4132afce47d475d2be3ea436ca630a145ad` (2026-10-20T11:21:40+01:00) |
| Commit subject | Merge pull request #42 from SoC-Labs/chore/repo-url-rename |
| Last pyverify change at or before it | `2e22d010bd624fd3598714515b0674b792771c23` |
| Source | `git archive <commit> host/pyverify` (committed files only) |
| Built with | `pip wheel --no-deps`, pip 26.2.1, Python 3.11.13, SOURCE_DATE_EPOCH=1792491700 |

Check it: `sha256sum vendor/mps3_pyverify-2.2.1-py3-none-any.whl` must print the sha256 above.
`tests/unit/test_l5_release.py` checks this in `make check`.

## The MPS3 OpenOCD configs (openocd/)

`openocd/` (shipped in the wheel as `src/harness_manager_mps3/openocd_cfg/`) is `host/openocd` from the same platform commit (last changed at
`6edf2b6990b9cac716b274fee7cadae257dcae0b`). The MPS3 pack's debug service needs these target configs. From a
checkout next to the platform repo it finds them there; anywhere else it uses the packaged copy.
`HARNESS_MANAGER_MPS3_OPENOCD_DIR` still overrides both.

| File | sha256 |
|---|---|
| `openocd/README.md` | `e9deae515e4affc1c1e0ce7a65cc9639e8ff597a3911f5b5a04a999b740e10ec` |
| `openocd/nanosoc_iice_chain.cfg` | `104617b30bd026bfe60eeaf867d42cd7af279484c5d521568e3c68995c8c6e4f` |
| `openocd/nanosoc_mps3_jtag.cfg` | `27d71702427d5034c7551cf30c9ee94cc89f572d7b7e6135cfee886136489054` |
| `openocd/nanosoc_mps3_multicore_jtag.cfg` | `b40153b4795a60a690fb1b6298abd635dc48fbd2aaa811660836e5604382ca71` |
| `openocd/nanosoc_ops.tcl` | `3c9a0736e31e54b4230bf33c4f46dffd76f10c1e7ac930bfebce4204a7e97ea6` |
| `openocd/nanosoc_qs_mps3_jtag.cfg` | `6e896147b0818a90dc3164ea0aee71f5277121e5e883e3570531b2ad25abb52b` |
