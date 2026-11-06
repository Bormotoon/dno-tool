"""Контраст выбранных строк с использованием действующей палитры темы."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem


def luminance(color: QColor) -> float:
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: QColor, second: QColor) -> float:
    low, high = sorted((luminance(first), luminance(second)))
    return (high + 0.05) / (low + 0.05)


class KdeItemDelegate(QStyledItemDelegate):
    """Подбирает читаемый текст выделения без изменения общей темы приложения."""

    def initStyleOption(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        palette = QPalette(option.palette)
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            background = palette.color(group, QPalette.ColorRole.Highlight)
            candidates = [
                palette.color(group, role)
                for role in (
                    QPalette.ColorRole.Text,
                    QPalette.ColorRole.Base,
                    QPalette.ColorRole.WindowText,
                    QPalette.ColorRole.BrightText,
                )
            ]
            readable = max(candidates, key=lambda color: contrast(background, color))
            for _ in range(20):
                if contrast(background, readable) >= 4.5:
                    break
                readable = (
                    readable.darker(110)
                    if luminance(background) > luminance(readable)
                    else readable.lighter(110)
                )
            palette.setColor(group, QPalette.ColorRole.HighlightedText, readable)
        option.palette = palette
