"""Arm MPS3 (V2M-MPS3, HBI0309C) board pack, the pilot pack for Harness Manager.

The shell protocol is spoken ONLY through ``pyverify`` (mps3-nanosoc-platform
host/pyverify), which is the single host codec for net-protocol.md. This pack
adapts pyverify to the board-agnostic interfaces in ``socharness.core.pack``.
"""
