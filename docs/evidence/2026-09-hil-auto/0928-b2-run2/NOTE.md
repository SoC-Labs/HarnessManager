# NOTE: the Harness Manager commit this run used

The run did not record the HM commit. It records only `harness-manager version`, `0.1.0`
(`iter-*/0_hm_version.json`). It ran from the main checkout (`~/SoCLabs/harness-manager`) at
**`ddc8b24`** (Merge team/v7-align).

- **The run:** 2026-09-28 21:13:01 to 2026-09-29 08:20:43 BST (`summary.json`).
- **The checkout:** `main` was `ddc8b24` from 28 Sep 19:52:43 until 29 Sep 08:21:59 BST. It was
  then fast-forwarded to `c43b08e` (merge integ/0929), after the run ended. Source: the
  branch reflog, `git reflog show main --date=iso`.
- **The commands ran in the command-line process, not the service:** `0_hm_version.json` says
  engine `harness_manager.engine.Engine`. So every round ran `ddc8b24`'s code.
- **Why D4a shows "sensor identity unverified":** `ddc8b24` is before FIX-PACK-2, which
  `c43b08e` brought in. Its item 5 (`90207ba`) changed the MCC temperature text to "the SLR0 die
  diode via U53 (MPS3 schematic p4); not ambient".

The generated `REPORT.md` files are left as the runner wrote them. The runner is being changed
to record the commit itself.
