"""Контроллер видимых потоков камер с ограничением числа декодеров."""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from src.domain.models.camera import CameraModel
from src.ui.dialogs.tools.camera_qt_preview import QtCameraPreview
from src.ui.dialogs.tools.camera_video_output import CameraVideoOutput
from src.utils.system.camera_decoding import CameraDecoder
from src.workers.camera_preview import CameraPreviewWorker


class CameraPreviewController(QObject):
    """Удерживать потоки до finished и отбрасывать кадры старой конфигурации."""

    updated = Signal(str, object, float, str)
    notice = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._wanted: dict[str, tuple[CameraModel, tuple[int, int]]] = {}
        self._workers: dict[str, CameraPreviewWorker | QtCameraPreview] = {}
        self._backend = "ffmpeg"
        self._transport = "tcp"
        self._fps = 25
        self._decoders: dict[str, CameraDecoder] = {}
        self._last_notice = ""
        self._reported: dict[str, tuple[float, str, int]] = {}
        self._outputs: dict[str, CameraVideoOutput] = {}
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll)

    def _matches(
        self,
        worker: CameraPreviewWorker | QtCameraPreview,
        target: tuple[CameraModel, tuple[int, int]],
    ) -> bool:
        camera, _size = target
        if isinstance(worker, QtCameraPreview):
            if (
                self._backend != "qt"
                or worker.transport != self._transport
                or worker.output is not self._outputs.get(camera.identifier)
            ):
                return False
        elif self._backend != "ffmpeg":
            return False
        return worker.camera.rtsp_url == camera.rtsp_url

    def set_cameras(
        self,
        cameras: list[CameraModel],
        size: tuple[int, int],
        backend: str = "ffmpeg",
        outputs: dict[str, CameraVideoOutput] | None = None,
        transport: str = "tcp",
        *,
        fps: int = 25,
        sizes: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        if backend not in ("ffmpeg", "qt"):
            raise ValueError("Неизвестный бэкенд просмотра камер")
        if transport not in ("tcp", "udp"):
            raise ValueError("Неизвестный транспорт просмотра камер")
        if type(fps) is not int or fps not in (1, 3, 12, 25):
            raise ValueError("Частота просмотра: 1, 3, 12 или 25 кадров/с")
        self._backend = backend
        self._transport = transport
        self._fps = fps
        self._outputs = dict(outputs or {})
        self._wanted = {}
        for camera in cameras[:16]:
            try:
                _ = camera.rtsp_url
            except ValueError:
                self.updated.emit(
                    camera.identifier, None, 0.0, "Некорректный адрес RTSP"
                )
            else:
                self._wanted[camera.identifier] = (
                    camera,
                    (sizes or {}).get(camera.identifier, size),
                )
        self._reconcile()

    def _reconcile(self) -> None:
        for iid, worker in list(self._workers.items()):
            target = self._wanted.get(iid)
            if target is None or not self._matches(worker, target):
                worker.stop()
                if worker.isFinished():
                    del self._workers[iid]
                    worker.deleteLater()
            elif not worker.cancelled.is_set():
                worker.set_presentation(target[1], self._fps)
        # Завершающиеся декодеры тоже входят в лимит: быстрое переключение
        # страниц не создаёт неограниченную очередь процессов.
        for iid, (camera, size) in self._wanted.items():
            if iid not in self._workers and len(self._workers) < 16:
                decoder = self._decoders.setdefault(camera.rtsp_url, CameraDecoder())
                if self._backend == "qt":
                    output = self._outputs.get(iid)
                    if output is None:
                        continue
                    worker = QtCameraPreview(
                        camera,
                        size,
                        output,
                        self,
                        transport=self._transport,
                        fps=self._fps,
                        decoder=decoder,
                    )
                else:
                    worker = CameraPreviewWorker(
                        camera, size, self, fps=self._fps, decoder=decoder
                    )
                worker.set_presentation(size, self._fps)
                self._workers[iid] = worker
                worker.finished.connect(self._finished)
                worker.start()
        if self._workers:
            self._timer.start()
        else:
            self._timer.stop()

    @Slot()
    def _finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, (CameraPreviewWorker, QtCameraPreview)):
            return
        iid = worker.camera.identifier
        target = self._wanted.get(iid)
        # При ошибке сохраняем финальное сообщение до явного переключения.
        if target and not worker.cancelled.is_set() and self._matches(worker, target):
            image, received, state = worker.snapshot()
            self.updated.emit(iid, image, received, state)
            return
        if self._workers.get(iid) is worker:
            del self._workers[iid]
        worker.deleteLater()
        self._reconcile()

    def _poll(self) -> None:
        for iid, worker in list(self._workers.items()):
            target = self._wanted.get(iid)
            if (
                target
                and not worker.cancelled.is_set()
                and self._matches(worker, target)
            ):
                image, received, state = worker.snapshot()
                report = (received, state, int(time.monotonic()))
                if image is not None or self._reported.get(iid) != report:
                    self._reported[iid] = report
                    self.updated.emit(iid, image, received, state)
        warnings = sorted(
            {
                worker.decoder.warning
                for worker in self._workers.values()
                if worker.decoder.warning and not worker.cancelled.is_set()
            }
        )
        notice = "\n".join(warnings)
        if notice != self._last_notice:
            self._last_notice = notice
            self.notice.emit(notice)

    def stop(self) -> None:
        self._wanted.clear()
        self._outputs.clear()
        for iid, worker in list(self._workers.items()):
            worker.stop()
            if worker.isFinished():
                del self._workers[iid]
                worker.deleteLater()
        if not self._workers:
            self._timer.stop()

    def has_pending_workers(self) -> bool:
        return any(worker.isRunning() for worker in self._workers.values())
