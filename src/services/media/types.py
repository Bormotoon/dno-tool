"""Контракты локальных медиазаданий без объектов интерфейса."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar
from uuid import uuid4


@dataclass(frozen=True)
class RecordOptions:
    """Параметры записи камеры: кодирование и устойчивость к обрывам."""

    video: str = "copy"  # copy | h264 | h265
    hardware: bool = False  # Пробовать аппаратный кодер, иначе программный
    crf: int = 23
    audio: str = "auto"  # auto | aac | opus | flac | pcm | none
    preset: str = "fast"  # fast | balanced | quality
    resolution: str = "source"  # source | потолок WxH, только уменьшение
    gop: int = 2  # Секунды между ключевыми кадрами
    max_fps: int = 0  # 0 = без ограничения
    video_bitrate: int = 0  # кбит/с, 0 = режим CRF
    audio_bitrate: int = 128  # кбит/с для AAC/Opus
    audio_rate: int = 0  # Гц, 0 = как в потоке
    audio_channels: int = 0  # 0 = как в потоке
    reconnect_grace: float = 0

    @classmethod
    def from_policy(cls, policy: object | None) -> RecordOptions:
        if policy is None:
            return cls()
        return cls(
            video=getattr(policy, "record_video", "copy"),
            hardware=getattr(policy, "record_encoder", "cpu") == "gpu",
            crf=getattr(policy, "record_crf", 23),
            audio=getattr(policy, "record_audio", "auto"),
            preset=getattr(policy, "record_preset", "fast"),
            resolution=getattr(policy, "record_resolution", "source"),
            gop=getattr(policy, "record_gop", 2),
            max_fps=getattr(policy, "record_max_fps", 0),
            video_bitrate=getattr(policy, "record_video_bitrate", 0),
            audio_bitrate=getattr(policy, "record_audio_bitrate", 128),
            audio_rate=getattr(policy, "record_audio_rate", 0),
            audio_channels=getattr(policy, "record_audio_channels", 0),
            reconnect_grace=getattr(policy, "reconnect_grace", 0),
        )


class OperationStatus(str, Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class MediaProgress:
    stage: str
    media_time: float = 0.0
    duration: float | None = None
    frames: int = 0
    bytes_written: int = 0


@dataclass(frozen=True)
class MediaError:
    category: str
    stage: str
    message: str
    ffmpeg_code: int | None = None
    # Очищенный текст нативной ошибки: для журнала и диагностики.
    detail: str = ""


class MediaOperationError(RuntimeError):
    """Безопасная ошибка, которую разрешено показать пользователю."""

    def __init__(self, error: MediaError) -> None:
        super().__init__(error.message)
        self.error = error


T = TypeVar("T")


@dataclass(frozen=True)
class MediaResult(Generic[T]):
    status: OperationStatus
    value: T | None = None
    error: MediaError | None = None


@dataclass
class OperationContext:
    """Поток операции владеет PyAV; другие потоки меняют только отмену.

    Короткий commit_lock упорядочивает отмену и атомарную публикацию.
    Один контекст предназначен для одного верхнеуровневого задания.
    """

    cancelled: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    task_id: str = field(default_factory=lambda: uuid4().hex)
    progress: Callable[[MediaProgress], None] | None = None
    commit_lock: threading.RLock = field(default_factory=threading.RLock)
    _terminal: bool = field(default=False, init=False)

    @classmethod
    def with_timeout(
        cls,
        seconds: float,
        cancelled: threading.Event | None = None,
        progress: Callable[[MediaProgress], None] | None = None,
    ) -> OperationContext:
        return cls(
            cancelled=cancelled if cancelled is not None else threading.Event(),
            deadline=time.monotonic() + seconds,
            progress=progress,
        )

    def cancel(self) -> None:
        with self.commit_lock:
            if not self._terminal:
                self.cancelled.set()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Обработка отменена.")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("Превышено время обработки медиа.")

    def report(self, progress: MediaProgress) -> None:
        self.check()
        if self.progress is not None:
            self.progress(progress)

    def finish(self, result: MediaResult[T]) -> MediaResult[T]:
        with self.commit_lock:
            if self._terminal:
                raise RuntimeError("Медиазадание уже завершено.")
            self._terminal = True
            if self.cancelled.is_set():
                return MediaResult(OperationStatus.CANCELLED)
            return result
