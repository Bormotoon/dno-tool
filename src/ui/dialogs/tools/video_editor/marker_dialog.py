"""Редактор именованных точек и диапазонов проекта."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QMessageBox,
)
from src.domain.video.markers import VideoMarker


class MarkerDialog(QDialog):
    seek = Signal(float)

    def __init__(
        self,
        markers: list[VideoMarker],
        duration: float,
        position: float,
        mark_range: tuple[float, float] | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Маркеры и фрагменты")
        self.resize(760, 420)
        self.duration, self.position, self.mark_range = duration, position, mark_range
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Имя", "Начало, с", "Конец, с (0 = точка)", "Цвет", "Заметка"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table)
        row = QHBoxLayout()
        for title, callback in (
            (
                "Точка у курсора",
                lambda: self.add(VideoMarker(start=min(self.position, self.duration))),
            ),
            ("Диапазон In/Out", self.add_range),
            ("Перейти", self.go),
            ("Удалить", lambda: self.table.removeRow(self.table.currentRow())),
        ):
            button = QPushButton(title, self)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for marker in markers:
            self.add(marker)

    def add(self, marker: VideoMarker) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate(
            (marker.name, marker.start, marker.end, marker.color, marker.note)
        ):
            item = QTableWidgetItem(str(value))
            item.setData(Qt.ItemDataRole.UserRole, marker.iid)
            self.table.setItem(row, column, item)
        self.table.setCurrentCell(row, 0)

    def add_range(self) -> None:
        if (
            self.mark_range
            and 0 <= self.mark_range[0] < self.mark_range[1] <= self.duration
        ):
            self.add(VideoMarker(start=self.mark_range[0], end=self.mark_range[1]))
        else:
            QMessageBox.information(
                self, "Фрагменты", "Сначала задайте In/Out на таймлайне."
            )

    def values(self) -> list[VideoMarker]:
        result = []
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text() for column in range(5)]
            marker = VideoMarker(
                iid=self.table.item(row, 0).data(Qt.ItemDataRole.UserRole),
                name=values[0],
                start=float(values[1].replace(",", ".")),
                end=float(values[2].replace(",", ".")),
                color=values[3],
                note=values[4],
            )
            marker.validate(self.duration)
            result.append(marker)
        return result

    def go(self) -> None:
        try:
            markers = self.values()
            row = self.table.currentRow()
            if 0 <= row < len(markers):
                self.seek.emit(markers[row].start)
        except ValueError as error:
            QMessageBox.warning(self, "Маркеры", str(error))

    def accept(self) -> None:
        try:
            self.values()
        except ValueError as error:
            QMessageBox.warning(self, "Маркеры", str(error))
            return
        super().accept()
