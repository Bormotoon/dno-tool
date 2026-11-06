"""Очередь видеовывода ёмкостью один кадр с доставкой только в GUI."""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtMultimedia import QVideoFrame


class FrameMailbox(QObject):
    available = Signal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._frame: QVideoFrame | None = None
        self._scheduled = False
        self.received = 0
        self.dropped = 0
        self._offered_at = 0.0
        self.last_delivery_ms = 0.0
        self.max_pending = 0
        self._closed = False

    @Slot(QVideoFrame)
    def offer(self, frame: QVideoFrame) -> None:
        with self._lock:
            if self._closed:
                return
            self.received += 1
            if self._frame is not None:
                self.dropped += 1
            previous, self._frame = self._frame, QVideoFrame(frame)
            self._offered_at = time.monotonic()
            self.max_pending = 1
            emit = not self._scheduled
            self._scheduled = True
        del previous
        if emit:
            self.available.emit()

    def take(self) -> QVideoFrame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            if frame is not None:
                self.last_delivery_ms = (time.monotonic() - self._offered_at) * 1000
            self._scheduled = False
            return frame

    def clear(self) -> None:
        """Отпустить последний кадр до остановки декодера и его поверхности."""
        with self._lock:
            previous, self._frame = self._frame, None
            self._scheduled = False
        del previous

    def close(self) -> None:
        """Закрыть приём до остановки Qt; кадры освобождаются вне lock."""
        with self._lock:
            self._closed = True
        self.clear()
