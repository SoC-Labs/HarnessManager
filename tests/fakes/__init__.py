"""Board fakes shared by every team. Owned by the lead; extend through docs/CONTRACTS.md.

- ``virtual_board.VirtualMps3``: one MPS3 made of pyverify's FakeShell
  (Ethernet), ``FakeMcc`` (board-controller console) and ``FakeSdVolume``
  (configuration SD). Firmware *profiles* pin behaviour to a real harness
  release, so tests fail when the engine assumes a verb the fielded firmware
  lacks.
"""
