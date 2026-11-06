"""Живой RTSP: независимый декодер и один последний кадр на камеру."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from av import VideoFrame

from PySide6.QtCore import QObject, QThread
from PySide6.QtGui import QImage

from src.domain.models.camera import CameraModel
from src.services.media.camera_presentation import (
    CameraFrameScheduler,
    camera_frame_size,
)
from src.utils.system.camera_decoding import CameraDecoder


class CameraPreviewWorker(QThread):
    """Чтение никогда не ждёт GUI; старый кадр заменяется новым без очереди."""

    def __init__(
        self,
        camera: CameraModel,
        size: tuple[int, int],
        parent: QObject | None = None,
        *,
        fps: int = 25,
        decoder: CameraDecoder | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera = camera
        self.size = size
        self.decoder = decoder or CameraDecoder()
        self._frames: CameraFrameScheduler[VideoFrame] = CameraFrameScheduler(fps)
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._image: QImage | None = None
        self._received = 0.0
        self._activity = time.monotonic()
        self._state = "Подключение…"

    def stop(self) -> None:
        self.cancelled.set()

    def set_presentation(self, size: tuple[int, int], fps: int) -> None:
        """Обновить предел размера и частоту без остановки RTSP."""
        with self._lock:
            self.size = size
        self._frames.configure(fps)

    def snapshot(self) -> tuple[QImage | None, float, str]:
        with self._lock:
            image, self._image = self._image, None
            return image, self._received, self._state

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def run(self) -> None:
        from src.services.media import OperationContext, get_media_service
        from src.services.media.backend import frame_to_qimage

        delay = 1.0
        while not self.cancelled.is_set():
            self._set_state("Подключение…")
            received = False
            frames = None
            frame: VideoFrame | None = None
            selected: VideoFrame | None = None
            try:
                frames = get_media_service().camera_frames(
                    self.camera.rtsp_url,
                    None,
                    OperationContext(cancelled=self.cancelled),
                    decoder=self.decoder,
                )
                for frame in frames:
                    now = time.monotonic()
                    with self._lock:
                        self._received = self._activity = now
                        size = self.size
                    self._frames.offer(frame, now)
                    selected = self._frames.take_due(now)
                    if selected is None:
                        continue
                    width, height = camera_frame_size(
                        (selected.width, selected.height), size, size
                    )
                    selected = selected.reformat(
                        width=width, height=height, format="rgb24", threads=1
                    )
                    image = frame_to_qimage(selected)
                    image.setText("camera.source_size", f"{frame.width}x{frame.height}")
                    with self._lock:
                        self._image = image
                        self._state = "Прямой эфир"
                    received = True
            except InterruptedError:
                return
            except Exception:  # noqa: BLE001
                # Граница нативного декодера; URL ошибки не раскрывается.
                if self.decoder.policy.device_type and not self.decoder.failed:
                    self.decoder.fallback()
                    continue
            finally:
                self._frames.clear()
                if frames is not None:
                    frames.close()
                # Последняя аппаратная поверхность не должна удерживать контекст при повторе на CPU.
                frame = selected = None
            if self.cancelled.is_set():
                return
            delay = 1.0 if received else min(delay * 2, 15.0)
            self._set_state(f"Нет потока • повтор через {int(delay)} с")
            if self.cancelled.wait(delay):
                return
