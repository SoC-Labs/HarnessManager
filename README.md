# SoC Labs Harness Manager

A board manager for SoC prototyping harnesses: find a board, attach to it, program partitions, open consoles, debug, reset, read telemetry, and update the harness and the app. The Arm MPS3 is the pilot board.

It runs **standalone** (one PC with the board on Ethernet, plus optionally its Debug USB) or **through fpgahub** in the lab.

```bash
make venv            # Python 3.11 venv; installs socharness + pyverify (editable)
make check           # lint + unit + integration tests against a virtual MPS3
.venv/bin/socharness info 192.168.10.101          # a real board (board window only)
.venv/bin/socharness --json info 192.168.10.101
```

- Design and rationale: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- How the work is split across agent teams: [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md)
- Interfaces every team codes against, and how to change them: [docs/CONTRACTS.md](docs/CONTRACTS.md)

Status: Wave 0 scaffold. `info` and `probe` work end to end against the virtual board.
