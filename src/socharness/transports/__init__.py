"""How the engine reaches a board.

- ``direct`` (Team T3/T4): sockets, serial ports (pyserial), mounted volumes,
  local processes.
- ``hub`` (Team T8): the fpgahub REST/SSE API, plus WebSocket tunnels for
  local ports.

The scaffold MPS3 pack talks to the shell through pyverify's sockets directly.
When T3 lands the transport interface, packs take a transport instead of raw
addresses, so the same pack runs standalone or behind fpgahub.
"""
