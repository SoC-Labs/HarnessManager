# Architecture

The full rationale is in the 2026-09-23 feasibility report. This page is the short version for people working in the code.

```
     harness-manager CLI          web UI (browser: harness_manager.web, no build step)
            \                      /  HTTP + WebSockets, token auth, 127.0.0.1
             \          harness-manager-daemon (daemon: one engine per user, jobs, API v1)
              \                  /
                     Engine (T1): probe, sessions, capability view, EventBus
                                  |
     services: deploy · console · debug · telemetry · update · xdc     (board-agnostic)
                                  |
          board packs (entry point "harness_manager.boards"): mps3 (pilot), later haps-sx, kr260 ...
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

- **One engine per user, behind harness-manager-daemon.** A board's lock belongs to one process, so the CLI and the web UI share the daemon's session (docs/API.md). `harness-manager ui` starts it and opens the browser; `harness-manager ui --demo` serves scripted boards with no hardware.
