"""Clocks and Board & XDC: placeholders that still say what the board can do today."""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QVBoxLayout, QWidget

from socharness.core import capabilities as C
from socharness.core.model import BoardInfo

from ..context import BoardContext
from ..style import level_label, role_label, set_level


class PlaceholderTab(QWidget):
    def __init__(self, ctx: BoardContext, title: str, text: str,
                 capabilities: tuple[str, ...] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._caps = capabilities
        v = QVBoxLayout(self)
        box = QGroupBox(title)
        bv = QVBoxLayout(box)
        self.text = role_label(text, "muted")
        bv.addWidget(self.text)
        self.cap_labels = {}
        for cap in capabilities:
            label = level_label("", "info")
            self.cap_labels[cap] = label
            bv.addWidget(label)
        v.addWidget(box)
        v.addStretch(1)
        ctx.info_changed.connect(self._on_info)

    def _on_info(self, info: BoardInfo | None) -> None:
        for cap, label in self.cap_labels.items():
            state = self.ctx.capability(cap)
            if state is None:
                label.setText(f"{self.ctx.title(cap)}: waiting for the capability view")
                continue
            available, why = state
            label.setText(f"{self.ctx.title(cap)}: "
                          + ("available on this board" if available else f"Cannot: {why}"))
            set_level(label, "ok" if available else "warning")


def clocks_tab(ctx: BoardContext) -> PlaceholderTab:
    return PlaceholderTab(
        ctx, "Clocks (placeholder)",
        "The clock controls are not in this build. DUT clock presets (25/50/100 MHz) arrive "
        "with the clock verb; board oscillators with the MCC driver.",
        (C.CLOCK_DUT, C.CLOCK_BOARD))


def board_xdc_tab(ctx: BoardContext) -> PlaceholderTab:
    return PlaceholderTab(
        ctx, "Board & XDC (placeholder)",
        "XDC export is not in this build. It arrives in Wave 2 (team T10): the RM kit (an "
        "out-of-context XDC by boundary group, a connectivity sheet, pblock facts) and the "
        "full-board three-file export.")
