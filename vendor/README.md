# Vendored wheels

pyverify is the only codec for the MPS3 shell protocol. It lives in the platform
repo (mps3-nanosoc-platform, `host/pyverify`) and is not on PyPI. Harness Manager
needs it at run time, so its wheel is kept here. The installer and CI install it
with `pip --find-links vendor`.

Do not edit the wheel by hand. Rebuild it with `scripts/vendor_pyverify.sh` (or
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
`tests/unit/test_l5_vendor.py` checks this in `make check`.
