"""Числовая настройка огибающих звукового клипа."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)
from src.domain.video.models import Clip


class ClipAudioDialog(QDialog):
    def __init__(self, clip: Clip, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Звук клипа")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.fields: dict[str, QDoubleSpinBox] = {}
        for key, title, value, maximum in (
            ("fade_in", "Нарастание, с:", clip.fade_in, clip.duration),
            ("fade_out", "Затухание, с:", clip.fade_out, clip.duration),
            (
                "normalization_gain",
                "Коэффициент нормализации:",
                clip.normalization_gain,
                100,
            ),
        ):
            field = QDoubleSpinBox(self)
            field.setDecimals(6)
            field.setRange(0, maximum)
            field.setValue(value)
            field.setAccessibleName(title)
            form.addRow(title, field)
            self.fields[key] = field
        layout.addLayout(form)
        note = QLabel(
            "Огибающие применяются к границам текущего клипа. Коэффициент умножается на громкость клипа и мастер; исходник не меняется.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, float]:
        return {key: field.value() for key, field in self.fields.items()}
