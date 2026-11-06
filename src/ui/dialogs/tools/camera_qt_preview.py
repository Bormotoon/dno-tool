"""Прямой вывод RTSP через Qt Multimedia без преобразования кадров в Python."""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import (
    QLoggingCategory,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
    qVersion,
)
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from shiboken6 import isValid

from src.domain.models.camera import CameraModel
from src.services.media.camera_presentation import CameraFrameScheduler
from src.ui.dialogs.tools.camera_video_output import CameraVideoOutput
from src.utils.system.camera_decoding import CameraDecoder, CameraDecoding
from src.utils.system.qt_multimedia import stop_media_player
from src.workers.camera_preview import CameraPreviewWorker


class QtCameraPreview(QObject):
    """Qt выполняет декодирование в своих потоках; GUI получает только время кадра."""

    finished = Signal()

    def __init__(
        self,
        camera: CameraModel,
        size: tuple[int, int],
        output: CameraVideoOutput,
        parent: QObject | None = None,
        *,
        transport: str = "tcp",
        fps: int = 25,
        decoder: CameraDecoder | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera = camera
        self.size = size
        self.output = output
        self.transport = transport
        self.decoder = decoder or CameraDecoder()
        self._fps = fps
        self._frames: CameraFrameScheduler[QVideoFrame] = CameraFrameScheduler(fps)
        self._cpu: CameraPreviewWorker | None = None
        self.cancelled = threading.Event()
        self._received = 0.0
        self._activity = time.monotonic()
        self._state = "Подключение…"
        self._finished = False
        self._delay = 1
        # Qt вызывает av_dump_format с полным RTSP URL при включённом info-логе.
        # Та же политика уже используется в видеоредакторе.
        QLoggingCategory.setFilterRules(
            "qt.multimedia.ffmpeg=false\nqt.multimedia.ffmpeg.*=false"
        )
        self._player = QMediaPlayer(self)
        if hasattr(self._player, "playbackOptions"):
            options = self._player.playbackOptions()
            # Достаточная буферизация нужна для камер с редкими ключевыми кадрами.
            options.setPlaybackIntent(options.PlaybackIntent.Playback)
            options.setProbeSize(10_000_000)
            options.setNetworkTimeout(10000)
            self._player.setPlaybackOptions(options)
        # Без QAudioOutput звук не воспроизводится.
        # Входной sink не выводит каждый кадр: отбор выполняется до GPU-рендера.
        self._sink = QVideoSink(self)
        self._sink.setRhi(output.videoSink().rhi())
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(
            self._frame, Qt.ConnectionType.DirectConnection
        )
        self._player.errorOccurred.connect(self._error)
        self._player.mediaStatusChanged.connect(self._status)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._connect)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._watch)
        self._finish_timer = QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self.finished)
        # Выход принадлежит плитке и может исчезнуть при смене раскладки.
        output.destroyed.connect(self.stop)

    def start(self) -> None:
        if self.output.software_output and self.decoder.policy.device_type:
            self.decoder.fallback()
        if self.decoder.failed:
            self._start_cpu()
            return
        self._watchdog.start()
        self._retry.start(0)

    def snapshot(self) -> tuple[None, float, str]:
        if self._cpu is not None:
            image, self._received, self._state = self._cpu.snapshot()
            if image is not None and isValid(self.output):
                self.output.present(QVideoFrame(image))
        else:
            if self._sink.rhi() is None and self.output.videoSink().rhi() is not None:
                self._sink.setRhi(self.output.videoSink().rhi())
            frame = self._frames.take_due()
            if frame is not None and isValid(self.output):
                self.output.present(frame)
            if self._frames.received_at:
                self._received = self._activity = self._frames.received_at
                if not self._retry.isActive():
                    self._state = "Прямой эфир"
                    self._delay = 1
        return None, self._received, self._state

    def set_presentation(self, size: tuple[int, int], fps: int) -> None:
        """Живое обновление вывода не меняет источник QMediaPlayer."""
        self.size, self._fps = size, fps
        self._frames.configure(fps)
        self.output.set_maximum(size)
        if self._cpu is not None:
            self._cpu.set_presentation(size, fps)

    def isRunning(self) -> bool:
        return not self._finished

    def isFinished(self) -> bool:
        return self._finished

    @Slot()
    def _connect(self) -> None:
        if self.cancelled.is_set():
            return
        self._received = 0.0
        self._frames.clear()
        self._state = "Подключение…"
        self._activity = time.monotonic()
        source = self.camera.rtsp_url
        self._sink.setRhi(self.output.videoSink().rhi())
        if QUrl(source).scheme() in ("rtsp", "rtsps"):
            if tuple(int(part) for part in qVersion().split(".")[:2]) < (6, 12):
                self._state = "Для просмотра RTSP обновите PySide6/Qt до 6.12"
                self._watchdog.stop()
                self._finished = True
                self._finish_timer.start(0)
                return
            os.environ["QT_FFMPEG_RTSP_TRANSPORT"] = self.transport
        self._player.setSource(QUrl(source))
        if not self._retry.isActive():
            self._player.play()

    @Slot(QVideoFrame)
    def _frame(self, frame: QVideoFrame) -> None:
        # DirectConnection: здесь нельзя обращаться к таймерам или виджетам GUI.
        if frame.isValid() and not self.cancelled.is_set():
            self._frames.offer(QVideoFrame(frame))

    @Slot()
    def _error(self) -> None:
        # errorString может содержать URL с паролем; показываем общий статус.
        if (
            self.decoder.policy.device_type
            and not self.decoder.failed
            and self._player.error()
            in (QMediaPlayer.Error.ResourceError, QMediaPlayer.Error.FormatError)
        ):
            self.decoder.fallback()
            self._start_cpu()
            return
        self._schedule_retry()

    def _start_cpu(self) -> None:
        """После отказа Qt/GPU продолжить CPU-декодером, сохранив видеовыход."""
        if self._cpu is not None or self.cancelled.is_set():
            return
        self._retry.stop()
        self._watchdog.stop()
        self._frames.clear(close=True)
        self._player.errorOccurred.disconnect(self._error)
        self._player.mediaStatusChanged.disconnect(self._status)
        stop_media_player(self._player)
        self._player.setVideoOutput(None)  # type: ignore[arg-type]  # Qt принимает nullptr.
        self._player.setSource(QUrl())
        self._sink.setVideoFrame(QVideoFrame())
        self._sink.setRhi(None)  # type: ignore[arg-type]  # Qt принимает nullptr.
        if isValid(self.output):
            self.output.clear_video()
        self._received = 0.0
        self._state = "Подключение…"
        self._cpu = CameraPreviewWorker(
            self.camera,
            self.size,
            self,
            fps=self._fps,
            decoder=CameraDecoder(CameraDecoding("cpu")),
        )
        self._cpu.finished.connect(self._cpu_finished)
        self._cpu.start()

    @Slot()
    def _cpu_finished(self) -> None:
        if self.cancelled.is_set():
            self._finished = True
            self._finish_timer.start(0)

    def _status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in (
            QMediaPlayer.MediaStatus.EndOfMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
        ):
            self._schedule_retry()

    def _watch(self) -> None:
        if not self._retry.isActive() and time.monotonic() - self._activity > 20:
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        if self.cancelled.is_set() or self._retry.isActive() or self._cpu is not None:
            return
        self._state = f"Нет потока • повтор через {self._delay} с"
        # Сначала активируем таймер, чтобы сигналы stop/setSource не запланировали повтор.
        self._retry.start(self._delay * 1000)
        self._delay = min(self._delay * 2, 15)
        stop_media_player(self._player)
        self._player.setSource(QUrl())
        self._frames.clear()

    @Slot()
    def stop(self) -> None:
        if self.cancelled.is_set():
            return
        self.cancelled.set()
        self._frames.clear(close=True)
        self._retry.stop()
        self._watchdog.stop()
        # Видеовыход может удалить sink до сигнала destroyed своего виджета.
        if isValid(self._sink):
            self._sink.videoFrameChanged.disconnect(self._frame)
        stop_media_player(self._player)
        self._player.setVideoOutput(None)  # type: ignore[arg-type]  # Qt принимает nullptr, stubs PySide6 — нет.
        self._player.setSource(QUrl())
        self._sink.setVideoFrame(QVideoFrame())
        self._sink.setRhi(None)  # type: ignore[arg-type]  # Qt принимает nullptr.
        if isValid(self.output):
            self.output.clear_video()
        if self._cpu is not None and self._cpu.isRunning():
            self._cpu.stop()
            return
        self._finished = True
        # Согласование контроллера не должно входить рекурсивно в обход реестра.
        self._finish_timer.start(0)
