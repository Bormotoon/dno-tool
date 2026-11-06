"""Точная числовая правка клипа вместо перетаскивания короткого блока."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QVBoxLayout,
)

from src.domain.video.models import Clip, ClipEdit


class ClipDialog(QDialog):
    def __init__(self, clip: Clip, track_iid: str, maximum: float, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Границы клипа, секунды")
        self.clip_iid = clip.iid
        self.track_iid = track_iid
        self.speed = clip.speed
        self.fields: dict[str, QDoubleSpinBox] = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key, label, value, limit in (
            ("start", "Начало на таймлайне:", clip.timeline_start, 1e9),
            ("in", "Вход исходника:", clip.src_in, maximum),
            ("out", "Выход исходника:", clip.src_out, maximum),
            ("duration", "Длительность:", clip.duration, maximum),
        ):
            field = QDoubleSpinBox()
            field.setDecimals(9)
            field.setRange(0, limit)
            field.setSingleStep(0.04)
            field.setValue(value)
            field.setAccessibleName(label)
            self.fields[key] = field
            form.addRow(label, field)
        self.fields["in"].valueChanged.connect(self._bounds_changed)
        self.fields["out"].valueChanged.connect(self._bounds_changed)
        self.fields["duration"].valueChanged.connect(self._duration_changed)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _bounds_changed(self) -> None:
        field = self.fields["duration"]
        field.blockSignals(True)
        field.setValue(max(0, self.fields["out"].value() - self.fields["in"].value()) / self.speed)
        field.blockSignals(False)

    def _duration_changed(self, value: float) -> None:
        self.fields["out"].setValue(self.fields["in"].value() + value * self.speed)
        self._bounds_changed()

    def values(self) -> ClipEdit:
        return ClipEdit(
            self.clip_iid,
            self.track_iid,
            self.fields["start"].value(),
            self.fields["in"].value(),
            self.fields["out"].value(),
        )
