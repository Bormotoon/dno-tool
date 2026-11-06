"""Удаление QThread только после полного завершения нативного потока."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QTimer, Slot

_REAPERS: set[VideoThreadReaper] = set()


class VideoThreadReaper(QObject):
    def __init__(
        self, thread: QThread, worker: QObject, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.thread_object, self.worker = thread, worker
        _REAPERS.add(self)
        thread.finished.connect(self.retire)

    @Slot()
    def retire(self) -> None:
        # finished предшествует освобождению TLS. Удаление QThread в этот
        # момент может ждать GIL, уже занятый GUI, и блокировать весь процесс.
        if not self.thread_object.wait(0):
            QTimer.singleShot(10, self.retire)
            return
        self.thread_object.deleteLater()
        self.worker = None
        _REAPERS.discard(self)
        self.deleteLater()
