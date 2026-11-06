"""Заметное сообщение об отказе правки над рабочей областью редактора."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget

NOTICE_TIMEOUT_MS = 12000
ERROR_STYLE = (
    "background: #b71c1c; color: white; padding: 4px 8px; border-radius: 3px;"
)


class NoticeBar(QFrame):
    """Показывает причину отказа: подсказка у виджета легко остаётся незамеченной."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoEditorNotice")
        self.setStyleSheet(f"#videoEditorNotice {{ {ERROR_STYLE} }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color: white;")
        layout.addWidget(self.label, 1)
        close = QToolButton(self)
        close.setText("✕")
        close.setAutoRaise(True)
        close.setStyleSheet("color: white;")
        close.setToolTip("Скрыть сообщение")
        close.setAccessibleName("Скрыть сообщение")
        close.clicked.connect(self.dismiss)
        layout.addWidget(close)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(NOTICE_TIMEOUT_MS)
        self._timer.timeout.connect(self.dismiss)
        self.hide()

    def show_error(self, text: str) -> None:
        self.label.setText(text)
        self.show()
        self._timer.start()

    def dismiss(self) -> None:
        self._timer.stop()
        self.label.clear()
        self.hide()
