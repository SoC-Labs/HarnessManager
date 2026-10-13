# Harness Manager app icon

To switch the master: `python packaging/icon/build_icons.py packaging/icon/candidates/B-harness.svg` (then commit `build/`, copy the SVG over `packaging/linux/harness-manager.svg` and `src/harness_manager/web/static/favicon.svg`).

- Candidates: `candidates/` (A board, B harness, C chip with LED); `--preview` redraws `candidates/preview.png`.
- `build/` holds the generated hicolor PNGs, `.ico`, `.icns` and the master SVG. `scripts/install.sh` and `scripts/install.ps1` install from it.
- Needs one SVG renderer (cairosvg, rsvg-convert, ImageMagick or PySide6) and Pillow for `--preview`. These are dev tools, not runtime dependencies.
