"""Фоновое сохранение кадра исходника с отменой и атомарной записью."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.utils.filesystem.fs_utils import ensure_user_owned
from src.services.media import get_media_service, OperationContext
from src.domain.video.file_output import AtomicOutput


class FrameExportWorker(QObject):
    """Одно извлечение; владелец удерживает поток до QThread.finished."""

    finished = Signal(str)
    error = Signal(str)
    settled = Signal()

    def __init__(
        self,
        source: str,
        seconds: float,
        output: str,
        generation: int,
        stream: int = 0,
        protected_sources: tuple[str, ...] = (),
        overwrite: bool = True,
        filter_chain: str = "",
    ) -> None:
        super().__init__()
        self.filter_chain = filter_chain
        self.source = source
        self.seconds = seconds
        self.output = output
        self.generation = generation
        self.stream = stream
        self.protected_sources = protected_sources or (source,)
        self.overwrite = overwrite
        self._cancelled = threading.Event()
        self._commit_lock = threading.Lock()
        self._thread: QThread | None = None

    def start(self, parent: QObject) -> None:
        thread = QThread(parent)
        self._thread = thread
        self.moveToThread(thread)
        thread.started.connect(self._run)
        self.settled.connect(thread.quit)
        self.settled.connect(self.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def thread(self) -> QThread | None:
        return self._thread

    def cancel(self) -> None:
        """Отмена и фиксация результата упорядочены одним коротким lock."""
        with self._commit_lock:
            self._cancelled.set()

    def _run(self) -> None:
        staging: str | None = None
        error = ""
        committed = False
        try:
            if self._cancelled.is_set():
                return
            source = Path(self.source)
            output = Path(self.output)
            if output.suffix.lower() != ".png":
                raise ValueError("Для кадра выберите расширение .png.")
            for protected in self.protected_sources:
                path = Path(protected)
                if path.resolve() == output.resolve() or (
                    path.exists() and output.exists() and os.path.samefile(path, output)
                ):
                    raise ValueError("Кадр нельзя записать поверх исходника.")
            before = source.stat()
            fd, staging = tempfile.mkstemp(
                prefix=".dno-frame-", suffix=".png", dir=output.parent
            )
            os.close(fd)
            get_media_service().frame_png(
                self.source,
                self.seconds,
                staging,
                self.stream,
                OperationContext.with_timeout(60, self._cancelled),
                filter_chain=self.filter_chain,
            )
            with open(staging, "rb") as frame:
                if frame.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("FFmpeg не создал PNG-кадр в выбранной позиции.")
            after = source.stat()
            if (before.st_mtime_ns, before.st_size) != (
                after.st_mtime_ns,
                after.st_size,
            ):
                raise RuntimeError("Исходник изменился во время извлечения кадра.")
            ensure_user_owned(staging)
            with open(staging, "r+b") as ready:
                os.fsync(ready.fileno())
            with self._commit_lock:
                if self._cancelled.is_set():
                    return
                AtomicOutput.publish(staging, self.output, self.overwrite)
                committed = True
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                if staging is not None:
                    Path(staging).unlink(missing_ok=True)
            except Exception as exc:
                error = error or str(exc)
            finally:
                if committed:
                    self.finished.emit(self.output)
                elif error and not self._cancelled.is_set():
                    self.error.emit(error)
                self.settled.emit()
