"""Составные поля редактора KDE с единым контрактом значений."""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression, Signal
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ColorField(QWidget):
    """Сохраняет исходную строку цвета до реального редактирования."""

    value_changed = Signal(str)

    def __init__(self, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value, self)
        self.edit.setPlaceholderText("R,G,B или R,G,B,A")
        self.edit.setAccessibleName("Значение цвета KDE")
        self.choose = QPushButton("Выбрать…", self)
        self.choose.setAccessibleName("Открыть выбор цвета")
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.choose)
        self.edit.textEdited.connect(self.value_changed)
        self.choose.clicked.connect(self._choose)

    def get_value(self) -> str:
        return self.edit.text()

    def _choose(self) -> None:
        parts = self.get_value().split(",")
        try:
            current = (
                QColor(*[int(value) for value in parts])
                if len(parts) in (3, 4)
                else QColor(self.get_value())
            )
        except (TypeError, ValueError):
            current = QColor()
        dialog = QColorDialog(current if current.isValid() else QColor("white"), self)
        dialog.setWindowTitle("Цвет KDE")
        dialog.setOption(
            QColorDialog.ColorDialogOption.ShowAlphaChannel, len(parts) == 4
        )
        if dialog.exec() == QColorDialog.DialogCode.Accepted:
            color = dialog.selectedColor()
            values = [color.red(), color.green(), color.blue()]
            if len(parts) == 4:
                values.append(color.alpha())
            value = ",".join(map(str, values))
            self.edit.setText(value)
            self.value_changed.emit(value)


class FontField(QWidget):
    """Выбирает удалённое семейство, сохраняя остальные компоненты QFont."""

    value_changed = Signal(str)

    def __init__(
        self,
        value: str,
        families: tuple[str, ...],
        parent: QWidget | None = None,
        *,
        qt_major: int = 6,
    ) -> None:
        super().__init__(parent)
        self._parts = value.split(",")
        if not value:
            self._parts = [
                "",
                "10",
                "-1",
                "5",
                "50" if qt_major == 5 else "400",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        self._raw = value
        self._size_changed = False
        self._weight_changed = False
        self._italic_changed = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout()
        outer.addLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        self.family = QComboBox(self)
        self.family.setEditable(True)
        family_edit = self.family.lineEdit()
        if family_edit is not None:
            family_edit.setValidator(
                QRegularExpressionValidator(
                    QRegularExpression(r"[^,\r\n]*"), self.family
                )
            )
        self.family.setAccessibleName("Семейство шрифта на удалённом хосте")
        self.family.addItems(list(families))
        self.family.setCurrentText(self._parts[0] if self._parts else "")
        self.point_size = QDoubleSpinBox(self)
        self.point_size.setRange(1.0, 512.0)
        self.point_size.setDecimals(3)
        self.point_size.setSuffix(" пт")
        self.point_size.setAccessibleName("Размер шрифта в пунктах")
        try:
            size = float(self._parts[1])
            self.point_size.setValue(size)
            supported = len(self._parts) >= 10 and 1 <= size <= 512
        except (IndexError, ValueError):
            supported = False
        layout.addWidget(self.family, 1)
        layout.addWidget(self.point_size)
        style = QHBoxLayout()
        self.weight = QComboBox(self)
        self.weight.setAccessibleName("Начертание шрифта")
        old_scale = (
            len(self._parts) >= 5
            and self._parts[4].isdigit()
            and int(self._parts[4]) < 100
        )
        for title, weight in (
            ("Тонкий", "25" if old_scale else "300"),
            ("Обычный", "50" if old_scale else "400"),
            ("Полужирный", "63" if old_scale else "600"),
            ("Жирный", "75" if old_scale else "700"),
        ):
            self.weight.addItem(title, weight)
        original_weight = self._parts[4] if len(self._parts) >= 5 else ""
        if self.weight.findData(original_weight) < 0:
            self.weight.addItem("Исходное: " + original_weight, original_weight)
        self.weight.setCurrentIndex(self.weight.findData(original_weight))
        self.italic = QCheckBox("Курсив", self)
        self.italic.setChecked(len(self._parts) >= 6 and self._parts[5] == "1")
        style.addWidget(self.weight, 1)
        style.addWidget(self.italic)
        outer.addLayout(style)
        self.family.setEnabled(supported)
        self.point_size.setEnabled(supported)
        self.weight.setEnabled(supported)
        self.italic.setEnabled(supported)
        if not supported:
            self.setToolTip(
                "Исходная строка шрифта не распознана. Она сохранится без преобразования."
            )
        self.family.currentTextChanged.connect(self._family_changed)
        self.point_size.valueChanged.connect(self._size_edited)
        self.weight.currentIndexChanged.connect(self._weight_edited)
        self.italic.toggled.connect(self._italic_edited)

    def get_value(self) -> str:
        if len(self._parts) < 10:
            return self._raw
        parts = list(self._parts)
        parts[0] = self.family.currentText()
        if self._size_changed:
            parts[1] = format(self.point_size.value(), ".12g")
        if self._weight_changed:
            parts[4] = str(self.weight.currentData())
        if self._italic_changed:
            parts[5] = "1" if self.italic.isChecked() else "0"
        return ",".join(parts)

    def _family_changed(self, value: str) -> None:
        self.value_changed.emit(self.get_value())

    def _size_edited(self, value: float) -> None:
        self._size_changed = True
        self.value_changed.emit(self.get_value())

    def _weight_edited(self, index: int) -> None:
        self._weight_changed = True
        self.value_changed.emit(self.get_value())

    def _italic_edited(self, checked: bool) -> None:
        self._italic_changed = True
        self.value_changed.emit(self.get_value())
