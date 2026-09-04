"""
redactor_common/gui/qmessagebox_style.py

QMessageBox sizes itself to fit its text, but a long line with no
natural wrap point (a path, an error string, a URL) can stretch it far
wider than the screen. One app-level stylesheet rule fixes every call
site at once -- see epub project v29, where this was originally
discovered fixing 51 separate Calibre error dialogs in one shot.

Usage, once at startup (in main.py, right after creating QApplication):
    from redactor_common.gui.qmessagebox_style import apply_message_box_style
    app = QApplication(sys.argv)
    apply_message_box_style(app)
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication

DEFAULT_MAX_WIDTH_PX = 480


def apply_message_box_style(app: QApplication, max_width_px: int = DEFAULT_MAX_WIDTH_PX) -> None:
    existing = app.styleSheet()
    rule = f"QMessageBox QLabel {{ max-width: {max_width_px}px; }}"
    app.setStyleSheet(f"{existing}\n{rule}" if existing else rule)
