"""Colours, levels and small rendering helpers shared by every tab.

A *level* is how the GUI colours a fact: ``ok``, ``warning``, ``error`` or
``info``. The level comes from what the engine reported (a ``Check``, an exit
code, an event topic); the GUI never invents one.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase
from PySide6.QtWidgets import QLabel, QTableWidgetItem, QWidget

from socharness.core.model import Check, LinkKind

OK = "#2e7d32"
WARNING = "#b26a00"
ERROR = "#c62828"
MUTED = "#6b6b6b"

LEVEL_COLOURS = {"ok": OK, "warning": WARNING, "error": ERROR, "info": ""}

# The item-data role that carries a table cell's level, so tests can read it.
LEVEL_ROLE = int(Qt.ItemDataRole.UserRole) + 1

STYLESHEET = f"""
QLabel[level="ok"] {{ color: {OK}; }}
QLabel[level="warning"] {{ color: {WARNING}; }}
QLabel[level="error"] {{ color: {ERROR}; }}
QLabel[role="reason"] {{ color: {WARNING}; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="rcline"] {{ font-weight: bold; }}
"""

LINK_NAMES = {
    LinkKind.ETHERNET: "Ethernet",
    LinkKind.USB_SERIAL: "USB serial",
    LinkKind.USB_MSD: "USB storage",
    LinkKind.USB_DEBUG: "USB debug",
    LinkKind.JTAG: "JTAG",
    LinkKind.HUB: "hub",
    LinkKind.SMART_POWER: "smart plug",
}


def check_level(check: Check) -> str:
    """Three states, three levels. UNCHECKED is a warning, never ``ok``."""
    return {Check.OK: "ok", Check.MISMATCH: "error"}.get(check, "warning")


def check_text(check: Check) -> str:
    return {
        Check.OK: "OK",
        Check.MISMATCH: "MISMATCH",
        Check.UNCHECKED: "UNCHECKED (could not compare; not a pass)",
    }[check]


def control_level(state: str) -> str:
    return {"idle": "ok", "busy": "warning", "wedged": "error", "offline": "error"}.get(
        state, "warning")


def set_level(widget: QWidget, level: str) -> None:
    """Tag a widget with a level; the stylesheet colours it."""
    widget.setProperty("level", level)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def level_label(text: str = "", level: str = "info", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    set_level(label, level)
    return label


def role_label(text: str, role: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", role)
    return label


def item(text: str, level: str = "info", tooltip: str = "") -> QTableWidgetItem:
    """A read-only table cell, coloured by level; the full text is its tooltip."""
    cell = QTableWidgetItem(text)
    cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    cell.setToolTip(tooltip or text)
    cell.setData(LEVEL_ROLE, level)
    colour = LEVEL_COLOURS.get(level, "")
    if colour:
        cell.setForeground(QBrush(QColor(colour)))
    return cell


def fixed_font() -> QFont:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def age_text(observed_at: float, now: float | None = None) -> str:
    age = max(0.0, (now if now is not None else time.time()) - observed_at)
    if age < 1.0:
        return "just now"
    if age < 120:
        return f"{age:.0f} s ago"
    if age < 7200:
        return f"{age / 60:.0f} min ago"
    return f"{age / 3600:.1f} h ago"
