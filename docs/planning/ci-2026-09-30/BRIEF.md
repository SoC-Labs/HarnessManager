# CI-PLAN shared brief (read first)

david (platform owner) asked on 2026-09-30 00:15:
"a CI system for future builds and stress testing we can run on an MPS3 board nightly. What is the best
way to handle this? What system should run the CI? How can we mutant test this? I want a system capable
of testing all our DUTs and functionality and to robustly test the capabilities of the harness and board
to catch bugs before releases of the harness, designs and the harness manager. I also need a system that
tests installs on fresh user environments by following a guide to ensure the documentation given is clear
and accurate for someone picking up and using this platform."

## The estate (facts, 2026-09-30)
- Repos:
  - PLATFORM: /home/dam1n19/SoCLabs/mps3-nanosoc-platform -> github SoC-Labs/MPS3-NanoSoC-Verification-Platform (private).
    master 3f7cea2 (Linux harness merged via PR #9). Worktree -lx = the Linux lead's (READ-ONLY for you).
    Guide worktree: /home/dam1n19/SoCLabs/mps3-nanosoc-platform-guide.
  - HM (Harness Manager, the desktop/CLI app): /home/dam1n19/SoCLabs/harness-manager -> github SoC-Labs/HarnessManager (private), main ab311b4.
  - fpgahub (the hub's board-sharing daemon): /home/dam1n19/SoCLabs/fpgahub.
- CI today: GitHub Actions on both repos. HM `make check` (~6000 tests, ~32 min) + `tests/web` (playwright, ~700,
  ~30 min); platform `make check-ci`. BILLING-BLOCKED since 24 Sep (every run red); gates run locally by hand.
  CI toolchain drift: CI had gcc 11/dtc 1.6.1 vs box gcc 8.
- Hardware: two Arm MPS3 (Kintex UltraScale KU115) boards behind the hub mapstone-dev.ecs.soton.ac.uk
  (fpgahub 0.3.0: leases per board, `sg fpga`, MCC console tty_00 single reader, config SD writes via MCC USB
  mass storage).
  - Board 1 = 192.168.10.101 (user microSD card present; Linux lead's soaks).
  - Board 2 = 192.168.11.101 (no card, netboots; HM's nightly HIL-AUTO).
  - No on-board FPGA JTAG cable. Everything goes over Ethernet via the hub.
  - MCC REBOOT = cold power-cycle (board 2 needs a person for PB0 if stage0 fails).
- DFX: a static shell (the "harness": MicroBlaze-V Linux harnessd, net-protocol v0.16-v0.18 on port 6900,
  6910 bitstream push, 6921 debug, 2542 XVC) + one reconfigurable partition (RP) for the DUT.
  - RC2 static 0x44EE76D5.
  - ~13 overlays: greybox, nanosoc, nanosoc_ila, nanosoc_multicore, nanosoc_upy, led, uart_echo, clcd_demo, dbg_demo,
    eth_ss, regdemo_a/b, socscope.
  - Bare-metal harness image also exists (0x72BB0A36).
- Existing test tooling to inventory:
  - HM `tools/hil` / `src/harness_manager/checks` (HIL-AUTO plans: linux, linux-netboot, linux-nocard,
    bare-metal; tiers read/safe/manual/skip; the app's Checks section runs them through the HM service);
  - the Linux lead's soak_linux.py (24 h gate soak, SSH p99), b2_keeper (auto re-netboot), stage0 rescue,
    boot-rate tools, `pyverify` (host/pyverify: the protocol codec + sweep/slot/swap tools);
  - platform `make check`/`check-ci` (sims, cocotb, VCS/Icarus, lint, regmap gates);
  - runbooks HIL_LINUX.md, HIL_B0.md, B0-B2 runbooks;
  - the guide agent's CLEAN_ACCOUNT_SYNTHESIS_RUN (P8: a fresh agent in a throwaway home follows the guide);
  - HM kit flow (kit v2 -> Vivado 2026.1 -> kit check/pack; ~30-60 min per RM build; Vivado licence on srv03335
    via /etc/profile.d).
- Machines:
  - srv03335 (16 cores, 251 GB, shared with other people's Innovus/VCS jobs; load often 20-60; Vivado 2026.1
    + 2024.1 at /research/CAD/Xilinx; hosts every agent session);
  - mapstone-dev (the hub: python3.6 system + /opt/fpgahub python3.11; sshd MaxStartups throttles bursts;
    systemd --user works there).
- Hard constraints:
  - Leases: agents share ONE fpgahub principal (david@mapstone-dev). Auto-mode blocks agents from acquiring leases.
    A CI needs its own principal/credential design.
  - A lease token was once committed to git in evidence: secrets hygiene matters.
  - Never write the DUT SST26 flash. Never share tty_00. Never retry an SD write mid-write.
    Board 2: no MCC REBOOT unattended until its stage0 bake is fielded.
  - Vendor IP under /research/AAA/ip_library/** is READ-ONLY (Arm IP licence; no Arm IP in public artefacts).
  - Release: Linux harness v2.0.0 + HM v0.1.0 + guide 1.0 ship Tue 6 Oct. The CI is for AFTER that.

## Rules for every CI-PLAN agent
- RESEARCH ONLY: read code/docs in the repos (read-only), use the web for tools/prior art.
  No board, no hub ssh, no fpgahub, no leases, no builds, no commits in any repo, no ports opened.
- Write your section as Markdown to /tmpdir/claude-74755/ci-plan/<your-lane>.md.
  - Decisions first: recommendation + 2-3 alternatives with one-line trade-offs.
  - Then specifics: what runs where, cadence, how long it takes, what it catches, what it costs, who owns it,
    first 3 concrete steps.
  - Cite the repo files you relied on (path:line) and web sources (URL).
  - Mark anything you're unsure of as such. Under ~1500 words; tables welcome.
- Hand back a 10-line summary + the file path.
