"""Настройки холста, одинаковые для preview и экспорта."""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from src.domain.video.models import ProjectCanvas


class CanvasDialog(QDialog):
    def __init__(self, canvas: ProjectCanvas, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Холст проекта")
        layout = QVBoxLayout(self)
        note = QLabel(
            "Авто: размер с учётом поворота и FPS первого видеоклипа на таймлайне. При вводе одной стороны вторая вычисляется с сохранением пропорций."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.width = QSpinBox()
        self.height = QSpinBox()
        for widget, value in ((self.width, canvas.width), (self.height, canvas.height)):
            widget.setRange(0, 16384)
            widget.setSpecialValueText("Авто")
            widget.setValue(value)
        self.fps = QLineEdit(
            str(Fraction(canvas.fps_num, canvas.fps_den)) if canvas.fps_num else ""
        )
        self.fps.setPlaceholderText("Авто; например, 25 или 30000/1001")
        self.fit = QComboBox()
        self.fit.addItem("Вписать с чёрными полями", "contain")
        self.fit.addItem("Заполнить с обрезкой", "cover")
        self.fit.setCurrentIndex(0 if canvas.fit == "contain" else 1)
        form.addRow("Ширина:", self.width)
        form.addRow("Высота:", self.height)
        form.addRow("FPS:", self.fps)
        form.addRow("Пропорции:", self.fit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> ProjectCanvas:
        fps = Fraction(self.fps.text().strip() or "0")
        if fps and not 1 <= fps <= 240:
            raise ValueError("FPS должен находиться в диапазоне 1..240.")
        if any(value % 2 for value in (self.width.value(), self.height.value())):
            raise ValueError("Для H.264/H.265 размеры должны быть чётными.")
        return ProjectCanvas(
            self.width.value(),
            self.height.value(),
            fps.numerator,
            fps.denominator,
            self.fit.currentData(),
        )

    def accept(self) -> None:
        try:
            self.values()
        except (ValueError, ZeroDivisionError):
            QMessageBox.warning(
                self,
                "Холст проекта",
                "Укажите чётные размеры и FPS от 1 до 240 (например, 30000/1001), либо оставьте автоматический выбор.",
            )
            return
        super().accept()
