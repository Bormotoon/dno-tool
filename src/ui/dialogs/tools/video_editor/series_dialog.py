"""Предварительный список результатов перед последовательным экспортом."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from src.domain.video.markers import VideoMarker
from src.domain.video.series import SeriesPlanner, SeriesTarget


class SeriesDialog(QDialog):
    def __init__(self, markers: list[VideoMarker], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Экспорт серии фрагментов")
        self.resize(720, 440)
        self.markers = markers
        self.targets: list[SeriesTarget] = []
        layout = QVBoxLayout(self)
        form = QFormLayout()
        folder = QHBoxLayout()
        self.directory = QLineEdit(self)
        browse = QPushButton("Папка…", self)
        browse.clicked.connect(self._browse)
        folder.addWidget(self.directory)
        folder.addWidget(browse)
        form.addRow("Папка результатов:", folder)
        self.template = QLineEdit("{index:03d}-{name}", self)
        form.addRow("Шаблон имени:", self.template)
        self.format = QComboBox(self)
        self.format.addItems(["mp4", "mkv", "wav", "mp3", "aac", "m4a"])
        form.addRow("Формат:", self.format)
        self.overwrite = QCheckBox(
            "Заменять существующие файлы после успешной проверки (иначе пропускать)",
            self,
        )
        self.overwrite.setChecked(False)
        layout.addLayout(form)
        layout.addWidget(self.overwrite)
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Файл результата", "Действие"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for signal in (
            self.directory.textChanged,
            self.template.textChanged,
            self.format.currentTextChanged,
            self.overwrite.toggled,
        ):
            signal.connect(self._update)
        self._update()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Папка результатов", self.directory.text()
        )
        if path:
            self.directory.setText(path)

    def _update(self) -> None:
        try:
            if not self.directory.text().strip():
                raise ValueError("Выберите папку результатов.")
            self.targets = SeriesPlanner.plan(
                self.markers,
                self.directory.text(),
                self.template.text(),
                self.format.currentText(),
                self.overwrite.isChecked(),
            )
            self.status.setText(
                "Последовательно, с теми же обработкой и диапазонами проекта. Видео: H.264 CRF 23 / medium; звук: AAC 192k (для WAV — PCM). Отмена текущего файла продолжает серию; «Проект → Отменить серию» останавливает всю очередь."
            )
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                any(not target.skip for target in self.targets)
            )
        except (ValueError, KeyError, IndexError) as error:
            self.targets = []
            self.status.setText(str(error))
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.table.setRowCount(len(self.targets))
        for row, target in enumerate(self.targets):
            self.table.setItem(row, 0, QTableWidgetItem(target.path))
            self.table.setItem(
                row,
                1,
                QTableWidgetItem(
                    "Пропустить существующий" if target.skip else "Экспортировать"
                ),
            )
        self.table.resizeColumnToContents(0)
