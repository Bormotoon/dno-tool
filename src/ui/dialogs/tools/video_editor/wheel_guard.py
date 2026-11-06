"""Колесо мыши над полем прокручивает панель, а не меняет значение монтажа."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QSlider, QWidget


class WheelGuard(QObject):
    """Число меняется колесом только в поле с фокусом, список и ползунок — никогда."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel:
            return False
        if isinstance(watched, QAbstractSpinBox) and watched.hasFocus():
            return False
        # Непринятое событие Qt передаёт родителю, то есть области прокрутки.
        event.ignore()
        return True


def guard_wheel(root: QWidget) -> None:
    """Защитить все поля внутри root; повторный вызов добавляет только новые."""
    guard = root.findChild(WheelGuard, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if guard is None:
        guard = WheelGuard(root)
    for kind in (QAbstractSpinBox, QComboBox, QSlider):
        for field in root.findChildren(kind):
            field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            field.installEventFilter(guard)
