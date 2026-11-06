"""Постраничная раскадровка итогового монтажа с отменяемой последовательной подготовкой."""

from __future__ import annotations

import threading

from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.domain.video.render_plan import RenderPlan
from src.workers.video_editor.preview_worker import PreviewRenderWorker
from src.services.media import get_media_service, OperationContext
from src.services.media.backend import frame_to_qimage


class StoryboardWorker(QThread):
    frame = Signal(int, QImage, str)

    def __init__(self, plan: RenderPlan, times: list[float], parent=None) -> None:
        super().__init__(parent)
        self.plan, self.times = plan, times
        self.cancelled = threading.Event()

    def run(self) -> None:
        control = OperationContext(cancelled=self.cancelled)
        for index, position in enumerate(self.times):
            if self.cancelled.is_set():
                return
            start = min(position, max(0, self.plan.duration - 0.04))
            worker = PreviewRenderWorker(
                self.plan, None, start, min(self.plan.duration, start + 0.04), index, []
            )
            worker.cancelled = self.cancelled
            try:
                worker.run()
                control.check()
                if worker.error:
                    raise ValueError(worker.error)
                frame = get_media_service().frame(worker.output, 0, 0, OperationContext.with_timeout(15, self.cancelled), filter_chain="scale=320:180:force_original_aspect_ratio=decrease")
                self.frame.emit(index, frame_to_qimage(frame), "")
            except InterruptedError:
                return
            except Exception as error:
                self.frame.emit(index, QImage(), str(error)[:300])
            finally:
                if worker.directory:
                    worker.directory.cleanup()


class StoryboardDialog(QDialog):
    seek = Signal(float)
    PAGE_SIZE = 16

    def __init__(
        self, plan: RenderPlan, entries: list[tuple[str, float]], parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Раскадровка монтажа")
        self.resize(760, 540)
        self.plan, self.entries = plan, entries
        self.page = 0
        self.worker: StoryboardWorker | None = None
        self.pending_close = False
        self.pending_page: int | None = None
        layout = QVBoxLayout(self)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.list = QListWidget(self)
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setIconSize(QSize(160, 90))
        self.list.setGridSize(QSize(180, 135))
        self.list.itemActivated.connect(
            lambda item: self.seek.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        layout.addWidget(self.list)
        row = QHBoxLayout()
        for label, callback in (
            ("Назад", lambda: self.change_page(-1)),
            ("Далее", lambda: self.change_page(1)),
            ("Перейти", self.go),
            ("Закрыть", self.reject),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self._load()

    def go(self) -> None:
        item = self.list.currentItem()
        if item:
            self.seek.emit(item.data(Qt.ItemDataRole.UserRole))

    def change_page(self, delta: int) -> None:
        page = max(0, min((len(self.entries) - 1) // self.PAGE_SIZE, self.page + delta))
        if page == self.page:
            return
        if self.worker:
            self.pending_page = page
            self.worker.cancelled.set()
        else:
            self.page = page
            self._load()

    def _load(self) -> None:
        self.list.clear()
        entries = self.entries[
            self.page * self.PAGE_SIZE : (self.page + 1) * self.PAGE_SIZE
        ]
        for name, position in entries:
            item = QListWidgetItem(f"{name[:28]}\n{position:.3f} с")
            item.setData(Qt.ItemDataRole.UserRole, position)
            self.list.addItem(item)
        self.status.setText(
            f"Страница {self.page+1}/{max(1, (len(self.entries)+self.PAGE_SIZE-1)//self.PAGE_SIZE)}. Подготовка итоговых кадров; двойной щелчок — переход."
        )
        worker = StoryboardWorker(self.plan, [time for _, time in entries], self)
        self.worker = worker
        worker.frame.connect(self._frame)
        worker.finished.connect(self._finished)
        worker.start()

    def _frame(self, index: int, image: QImage, error: str) -> None:
        if self.pending_close or self.pending_page is not None:
            return
        item = self.list.item(index)
        if item:
            if not image.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(image)))
            item.setToolTip(error or "Итоговый кадр с титрами, слоями и масками")

    def _finished(self) -> None:
        worker, self.worker = self.worker, None
        worker.deleteLater()
        if self.pending_close:
            super().reject()
        elif self.pending_page is not None:
            self.page, self.pending_page = self.pending_page, None
            self._load()
        else:
            self.status.setText(
                f"Страница {self.page+1}. Готово. Двойной щелчок — переход к кадру."
            )

    def reject(self) -> None:
        if self.worker:
            self.pending_close = True
            self.worker.cancelled.set()
            self.status.setText("Остановка подготовки…")
        else:
            super().reject()

    def closeEvent(self, event) -> None:
        if self.worker:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
