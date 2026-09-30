# Kit-built design deployed on silicon (board 2), 30 Sep 2026 00:46-00:48

The first design built end to end through Harness Manager's kit flow (RC2 kit v2, Vivado 2026.1,
lane KIT-NIGHT: docs/evidence/2026-09-29-kit-night/) programmed on a real board.

- Design: `minimal`, rm_id 0x0100F28A, static 0x44EE76D5; partial 1,244,136 B crc 0x5bab2009,
  clearing 67,748 B crc 0x0b0f0205.
- HM main cda3667, service restarted from a clean environment; `kit pack <receipt> --import` into the store.
- Board 2 (mps3_02_pl, 192.168.11.101), rc2_v7n, lease held by david; approved by david 30 Sep 00:20.

| File | Step | Result |
|---|---|---|
| 01_program_minimal.txt | `harness-manager program 192.168.11.101 minimal --yes` | preflight ok (static_usercode unchecked: needs JTAG); programmed in 35.3 s via tcp; verified |
| 02_info_minimal.json | `harness-manager --json info` | rm_id 0x0100f28a |
| 03_restore.txt | `harness-manager restore 192.168.11.101` | baseline 0x00000000 in 38.5 s; verified |
| 04_info_greybox.json | `harness-manager --json info` | rm_id 0x00000000 |

`restore` takes no `--yes` flag. The night run 0930-b2-run3 started right after on the same board.
