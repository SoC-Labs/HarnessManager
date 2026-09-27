# LCD mirror wire vectors (TCP 6940)

The board's own bytes for the lcd_mirror wire: a HELLO, one UPDATE per tile encoding
(FILL, PAL1, PAL2, RLE16, RAW), a keyframe of noise split across three UPDATEs, and both
refusal lines. They answer HM's request H1 (`docs/design/LCD_MIRROR.md` §11).

**Source**
- Repo: the platform, `mps3-nanosoc-platform`, branch `origin/feat/lcd-mirror`.
- Path: `src/linux_harness/sw/harnessd/tests/fixtures/lcdmirror_wire/`.
- Added in commit `d86ce81e2de20e1d659b50fc9c6018a07086a77f` ("harnessd: interim software LCD mirror").
- Copied on 2026-09-27 at the branch tip `429d5e1b98864003b8370b2256ec6b972ed8548d`.
- The git tree `135209c3bf4c43c1a74c74adf8534385d709a440` is identical at both commits.
- Generator: `src/linux_harness/sw/harnessd/tests/lcdmirror_vectors.c`, built on the
  board's own encoders (`lcdmirror_enc.c`, the code `mps3-lcdmirror` sends with).
- Contract: `docs/contracts/net-protocol.md` v0.15, "LCD mirror (TCP 6940)".

**The files are copied byte for byte. Never edit them.** To refresh them, copy the
directory again from a newer commit, then regenerate `SHA256SUMS` and update this README.

**`manifest.json`** is the board's, unchanged. It says what each vector must decode to,
as CRC-32s of the source pixels (the frame before encoding), so a decoder is judged on
the picture, not on the bytes:
- `update_*`: `seq`, `t_ms`, `status` and `owner`, plus `tiles` as
  `[idx, enc, payload len, CRC-32 of the tile's 512 bytes]`;
- `key_split_*`: `parts`, `seq_first`, `valid_count`, and `frame_crc` (the CRC-32 of the
  whole 320x240 frame, row-major, RGB565 LE);
- `refusal_*`: one JSON line each.

**`SHA256SUMS`** is HM's. It pins the copied files, so a corrupted copy fails before
anything is parsed.

**`.gitignore`** is HM's too. The repo's root `.gitignore` ignores `*.bin`, so this
directory un-ignores its vectors.

**Tests:**
- `tests/unit/test_lm1_display_wire.py` parses every vector, checks it against the
  manifest, and re-encodes it byte for byte. It also checks that a corrupted vector fails
  either the checksum or the parse.
- `tests/integration/test_lm1_display_service.py` replays HELLO and the split keyframe
  through the compositor.
