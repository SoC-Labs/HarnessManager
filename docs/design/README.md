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
| Board locate (Identify) | LOCATE | The Linux lead's `locate` (rc2_v7) blinks the CLCD backlight with an "IDENTIFY: <who>" banner; HM's sidebar/Board-tile button is built against it (5 s, 1 per 10 s, no lease). User LEDs are a later request. | [BOARD_LOCATE.md](BOARD_LOCATE.md) |
| Live LCD mirror | LCD-MIRROR | Pixel-exact mirror from an 8080 bus snooper behind the KVM (mint 4); interim harness-only shadow; one wire, HM built now. | [LCD_MIRROR.md](LCD_MIRROR.md) ([evidence](lcd_mirror/)) |
| Harness versions from the web | HARNESS-DIST | Publish with OTA's release tool; add a Harness versions catalogue; A/B config SD. | [HARNESS_DISTRIBUTION.md](HARNESS_DISTRIBUTION.md) |
| HM self-update | OTA | Finish T7: lead-run `make release`, daemon restart, rollback to the installed version. | [HM_SELF_UPDATE.md](HM_SELF_UPDATE.md) |
| Settings menu | SETTINGS | One schema + resolver (lock > env > user > machine > pack > default); hubs first-class with Test connection; keyring secrets with a 0600 fallback. Built by SET-CORE, SET-PACK, SET-HUBS and SET-API. | [SETTINGS.md](SETTINGS.md) ([spike evidence](../assessment/settings_spike_2026-09-25/spike_output.txt)) |
| Board identity | BOARD-ID | Detect label/IP/MAC clashes (board vs its hub record vs other boards); "Fix identity" = `identity_set` over the claim forward + the warm `reboot` verb, never MCC REBOOT; netboot refuses (stage0 bake). Feature-gated on `identity` (net-protocol v0.16). | [BOARD_IDENTITY.md](BOARD_IDENTITY.md) |

Decisions on these designs are tracked in the lead's checkpoint page:
<https://claude.ai/artifact/GsS4um7qyJLCupPfTDet8r>.
