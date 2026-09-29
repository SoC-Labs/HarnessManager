# Nightly CI for the MPS3 platform: research, 30 Sep 2026

Five research lanes answered david's request of 30 Sep 00:15: a nightly CI on the MPS3 boards that builds,
stress-tests and mutation-tests the harness, the designs (DUT overlays) and Harness Manager, and checks
that a new user can install and use the platform by following the guide. Plus performance benchmarking.
Research only: nothing here has been built yet. The work starts after the 6 Oct release.

| File | Question |
|---|---|
| [BRIEF.md](BRIEF.md) | The shared brief and the facts the lanes started from |
| [arch.md](arch.md) | What runs the CI, where, with which identity; board scheduling, secrets, recovery |
| [matrix.md](matrix.md) | What to test per commit, nightly, weekly and per release; stress tests tied to real bugs |
| [mutant.md](mutant.md) | Code mutation (Python, C, RTL) and a "mutant zoo" of broken designs the product must refuse |
| [fresh.md](fresh.md) | Executable docs and agent-driven new-user runs in clean environments; the doc drift gate |
| [perf.md](perf.md) | Performance metrics, budgets and nightly regression tracking for the harness and HM |

The combined plan with decisions is the artifact page linked from the HM lead's report.
