# Architecture

The full rationale is in the feasibility report (<https://claude.ai/artifact/T12zMEjmH8ybHBBvZmi4N5>). This page is the short version for people working in the code.

```
            socharness CLI            socharness-gui (PySide6)
                    \                   /
                     Engine (T1): probe, sessions, capability view, EventBus
                                  |
     services: deploy · console · debug · telemetry · update · xdc     (board-agnostic)
                                  |
          board packs (entry point "socharness.boards"): mps3 (pilot), later haps-sx, kr260 ...
                                  |
     transports:  direct (sockets · serial · SD volume · processes)  |  hub (fpgahub REST/SSE/WSS)
                                  |
     MPS3:  Ethernet → shell (6900 control via pyverify, 6910 push, 6921 JTAG, 6930–6932 UARTs)
            Debug USB → MCC console · FPGA UARTs · config SD (V2M-MPS3) · CMSIS-DAP
```

- **One engine, two transports.** The standalone app and the fpgahub integration run the same engine. The hub is a standalone install with a network front door.
- **Ethernet-only after the first install.** The capability view says exactly which features need the USB cable, and which need newer harness firmware.
- **pyverify is the only MPS3 shell codec.** It lives in the platform repo, together with `FakeShell`, the executable spec of the firmware.
- **Board packs keep the core board-agnostic.** A pack supplies discovery, the controller driver, the harness protocol, program methods, debug routes, telemetry sources, pin data and the bundle format.
