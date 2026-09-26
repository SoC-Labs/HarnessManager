# Design notes

Proposals from the 2026-09-24 design lanes. None of them is wired into the app yet. Spike
code lives in `tests/spikes/`, `tools/` and `scripts/spikes/`; it never ships in the wheel,
and `make check` does not run it (only `tests/unit/test_clcd_mock.py`, which is hermetic).

| Topic | Lane | Recommendation | File |
|---|---|---|---|
| XVC fabric debug | XVC | Brokered, lease-gated session: HM relays XVC over SSH and owns hw_server. | [XVC_DEBUG.md](XVC_DEBUG.md) |
| DUT build kit storage | KIT-STORE | Per-static signed kit on the release channel, cached by HM; not on SD. | [DUT_BUILD_KIT_STORAGE.md](DUT_BUILD_KIT_STORAGE.md) |
| DUT build guide | KIT-GUIDE | A "Build" section driving a generated `build_rm.tcl`; receipt binds partial to static. | [DUT_BUILD_GUIDE.md](DUT_BUILD_GUIDE.md) ([template](../../src/harness_manager/services/kit/templates/build_rm.tcl.template), built by KIT-CORE) |
| Front panel (CLCD) | CLCD-HM | Panel as a read-mostly second face of HM, Linux harness only; shared tokens. | [CLCD_ALIGNMENT.md](CLCD_ALIGNMENT.md) ([mock-ups](clcd/)) |
| Live LCD mirror | LCD-MIRROR | Pixel-exact mirror from an 8080 bus snooper behind the KVM (mint 4); interim harness-only shadow; one wire, HM built now. | [LCD_MIRROR.md](LCD_MIRROR.md) ([evidence](lcd_mirror/)) |
| Harness versions from the web | HARNESS-DIST | Publish with OTA's release tool; add a Harness versions catalogue; A/B config SD. | [HARNESS_DISTRIBUTION.md](HARNESS_DISTRIBUTION.md) |
| HM self-update | OTA | Finish T7: lead-run `make release`, daemon restart, rollback to the installed version. | [HM_SELF_UPDATE.md](HM_SELF_UPDATE.md) |

Decisions on these designs are tracked in the lead's checkpoint page:
<https://claude.ai/artifact/GsS4um7qyJLCupPfTDet8r>.
