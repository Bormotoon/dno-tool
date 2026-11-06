"""Диагностика встроенного медиадвижка PyAV и FFmpeg."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.services.media.runtime import MediaCapabilities
from src.workers.ffmpeg_helper import MediaDiagnosticsWorker


class FFmpegDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        local_path: str = "",
        *,
        reason: str = "",
        choose_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Медиадвижок PyAV / FFmpeg")
        self.resize(820, 650)
        self.setMinimumSize(620, 460)
        self.setSizeGripEnabled(True)
        self.worker: MediaDiagnosticsWorker | None = None
        self.available = False
        self._pending_result: int | None = None

        layout = QVBoxLayout(self)
        self.status = QLabel(reason or "Проверка встроенного медиадвижка…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)

        summary = QGroupBox("Установленный комплект")
        summary_layout = QVBoxLayout(summary)
        self.version = QLabel("Ожидание результата проверки…")
        self.version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary_layout.addWidget(self.version)
        purpose = QLabel(
            "PyAV и FFmpeg выполняют анализ, фильтры и экспорт видео. "
            "Видеоредактор использует Qt Multimedia для предпросмотра; способ просмотра RTSP-камер настраивается отдельно."
        )
        purpose.setWordWrap(True)
        summary_layout.addWidget(purpose)
        layout.addWidget(summary)

        self.capability_tabs = QTabWidget()
        self._capability_lists: dict[str, QPlainTextEdit] = {}
        for key, title in (
            ("encoders", "Кодировщики"),
            ("filters", "Фильтры"),
            ("formats", "Форматы файлов"),
            ("gpu", "Пробное кодирование"),
        ):
            listing = QPlainTextEdit()
            listing.setReadOnly(True)
            listing.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self._capability_lists[key] = listing
            self.capability_tabs.addTab(listing, title)
        layout.addWidget(self.capability_tabs, 1)

        row = QHBoxLayout()
        self.refresh = QPushButton("Проверить комплект")
        self.refresh.clicked.connect(lambda: self.start(False))
        row.addWidget(self.refresh)
        self.gpu = QPushButton("Проверить кодирование CPU / GPU")
        self.gpu.clicked.connect(lambda: self.start(True))
        row.addWidget(self.gpu)
        row.addStretch(1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        QTimer.singleShot(0, lambda: self.start(False))

    def start(self, check_gpu: bool = False) -> None:
        if self.worker is not None:
            return
        self.status.setText(
            "Проверка кодировщиков CPU / GPU…"
            if check_gpu
            else "Проверка библиотек и возможностей…"
        )
        if check_gpu:
            self._capability_lists["gpu"].clear()
            self.capability_tabs.setCurrentWidget(self._capability_lists["gpu"])
        worker = MediaDiagnosticsWorker(self, check_gpu=check_gpu)
        self.worker = worker
        worker.report.connect(self._report)
        worker.gpu_checked.connect(self._gpu_result)
        worker.finished.connect(self._finished)
        self.refresh.setEnabled(False)
        self.gpu.setEnabled(False)
        worker.start()

    def _report(self, snapshot: MediaCapabilities) -> None:
        self.available = True
        self.status.setText("Встроенный комплект PyAV / FFmpeg загружен и проверен.")
        self.version.setText(snapshot.version)
        self.version.setToolTip(snapshot.version)
        for key, values in (
            ("encoders", snapshot.encoders),
            ("filters", snapshot.filters),
            ("formats", snapshot.formats),
        ):
            listing = self._capability_lists[key]
            listing.setPlainText("\n".join(sorted(values)))
            index = next(
                i
                for i in range(self.capability_tabs.count())
                if self.capability_tabs.widget(i) is listing
            )
            title = {
                "encoders": "Кодировщики",
                "filters": "Фильтры",
                "formats": "Форматы файлов",
            }[key]
            self.capability_tabs.setTabText(index, f"{title} ({len(values)})")

    def _gpu_result(self, codec: str, device: str, error: str) -> None:
        if codec == "GPU":
            result = error
        else:
            result = f"{codec} — {device}: {error or 'кодирование выполнено'}"
        self._capability_lists["gpu"].appendPlainText(result)

    def _finished(self) -> None:
        worker = self.worker
        if worker is None:
            return
        if worker.error:
            self.status.setText("Проверка не пройдена: " + worker.error)
        elif self.available:
            self.status.setText(
                "Проверка завершена. Результат пробного кодирования показан на отдельной вкладке."
            )
        else:
            self.status.setText("Проверка завершена.")
        self.worker = None
        worker.deleteLater()
        self.refresh.setEnabled(True)
        self.gpu.setEnabled(True)
        if self._pending_result is not None:
            result, self._pending_result = self._pending_result, None
            super().done(result)

    def done(self, result: int) -> None:
        if self.worker is not None:
            self._pending_result = result
            self.worker.cancel()
            return
        super().done(result)


def show_media_diagnostics(parent: QWidget | None, reason: str) -> str | None:
    """Открыть диагностику встроенного медиадвижка."""
    dialog = FFmpegDialog(parent, reason=reason)
    dialog.exec()
    return "pyav" if dialog.available else None
