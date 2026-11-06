"""Сервисный фасад единственного локального движка PyAV."""

from __future__ import annotations

import errno
import threading
from collections.abc import Callable, Generator, Iterator
from typing import TYPE_CHECKING, BinaryIO, TypeVar, cast

if TYPE_CHECKING:
    from av import VideoFrame
    from src.utils.system.camera_decoding import CameraDecoder

from src.architecture import EventBus
from src.domain.video.failures import sanitize_detail
from src.domain.video.metadata import MetadataCache, ProbeMetadata
from src.services.base import BaseService

from .backend import PyAVBackend
from .render_types import RenderGraphPlan
from .runtime import MediaCapabilities, inspect_runtime
from .types import (
    MediaError,
    MediaOperationError,
    MediaResult,
    OperationContext,
    OperationStatus,
    RecordOptions,
)

T = TypeVar("T")
_service_lock = threading.RLock()


class MediaService(BaseService):
    """Singleton хранит только неизменяемые возможности; медиаконтексты — в задании."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        super().__init__(event_bus)
        self._backend = PyAVBackend()
        self._runtime: MediaCapabilities | None = None
        self._runtime_lock = threading.Lock()

    def capabilities(self) -> MediaCapabilities:
        with self._runtime_lock:
            if self._runtime is None:
                try:
                    self._runtime = inspect_runtime()
                except (ImportError, OSError) as error:
                    raise MediaOperationError(
                        MediaError(
                            "runtime",
                            "init",
                            "Не удалось загрузить встроенные медиабиблиотеки PyAV/FFmpeg. Проверьте поставку приложения.",
                            getattr(error, "errno", None),
                        )
                    ) from None
            return self._runtime

    @property
    def identity(self) -> str:
        return self.capabilities().identity

    def execute(
        self, operation: Callable[[], T], context: OperationContext, stage: str
    ) -> MediaResult[T]:
        """Единственная терминальная запись результата верхнеуровневой операции."""
        try:
            context.check()
            value = operation()
            context.check()
            result = MediaResult(OperationStatus.SUCCESS, value)
        except InterruptedError:
            result = MediaResult(OperationStatus.CANCELLED)
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            result = MediaResult(OperationStatus.ERROR, error=self.error(error, stage))
        return context.finish(result)

    @staticmethod
    def error(error: Exception, stage: str) -> MediaError:
        if isinstance(error, MediaOperationError):
            return error.error
        if isinstance(error, TimeoutError):
            return MediaError("timeout", stage, "Превышено время ожидания медиаданных.")
        code = getattr(error, "errno", None)
        if code in (errno.ETIMEDOUT, 1414092869):
            return MediaError(
                "timeout", stage, "Превышено время ожидания медиаданных.", code
            )
        if code == errno.ENOSPC:
            return MediaError(
                "disk", stage, "Недостаточно места для записи медиафайла.", code
            )
        # Исключения PyAV могут содержать URL, пароли и нативную диагностику.
        if type(error).__module__.startswith("av."):
            category = "auth" if code in (401, 403, 825242872, 858797304) else "media"
            return MediaError(
                category,
                stage,
                (
                    "Отказ авторизации камеры."
                    if category == "auth"
                    else "FFmpeg не смог прочитать или записать медиаданные."
                ),
                code,
                sanitize_detail(error),
            )
        return MediaError("operation", stage, str(error))

    def probe(self, path: str, context: OperationContext) -> ProbeMetadata:
        context.check()
        key = MetadataCache.key(path, self.identity)
        cached = MetadataCache.get(key)
        if cached is not None:
            return cached
        try:
            result = self._backend.probe(path, context)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "probe")) from None
        MetadataCache.put(key, result)
        return result

    def frame(
        self,
        path: str,
        seconds: float,
        stream: int,
        context: OperationContext,
        *,
        filter_chain: str = "",
    ) -> VideoFrame:
        self.capabilities()
        try:
            return self._backend.frame(
                path, seconds, stream, context, filter_chain=filter_chain
            )
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "frame")) from None

    def frame_png(
        self,
        path: str,
        seconds: float,
        output: str,
        stream: int,
        context: OperationContext,
        *,
        filter_chain: str = "",
    ) -> None:
        frame = self.frame(path, seconds, stream, context, filter_chain=filter_chain)
        self._backend.write_png(frame, output, context)

    def waveform(
        self, path: str, output: str, stream: int, context: OperationContext
    ) -> None:
        self.capabilities()
        try:
            self._backend.waveform(path, output, stream, context)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "waveform")) from None

    def peak(
        self,
        path: str,
        stream: int,
        start: float,
        end: float,
        context: OperationContext,
    ) -> float:
        self.capabilities()
        try:
            return self._backend.peak(path, stream, start, end, context)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "analyze")) from None

    def render(
        self,
        plan: RenderGraphPlan,
        output: str,
        context: OperationContext,
        *,
        max_bytes: int | None = None,
    ) -> None:
        self.capabilities()
        from .render import render_graph

        try:
            render_graph(plan, output, context, max_bytes=max_bytes)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "encode")) from None

    def remux(
        self,
        segments: list[tuple[str, float, float | None]],
        output: str,
        context: OperationContext,
        *,
        all_streams: bool = False,
        repair: bool = False,
        max_bytes: int | None = None,
    ) -> None:
        from .remux import remux_segments

        self.capabilities()
        try:
            remux_segments(
                segments,
                output,
                context,
                all_streams=all_streams,
                repair=repair,
                max_bytes=max_bytes,
            )
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "copy")) from None

    def verify(self, path: str, context: OperationContext) -> None:
        """Проверить весь медиарезультат декодированием с ограниченной памятью."""
        from src.utils.system.media_runtime import load_pyav

        self.capabilities()
        av = load_pyav()
        with av.open(path, timeout=(5.0, 1.0)) as container:
            streams = [s for s in container.streams if s.type in ("video", "audio")]
            counts = dict.fromkeys((s.index for s in streams), 0)
            for stream in streams:
                stream.codec_context.thread_count = 1
            for packet in container.demux(streams):
                context.check()
                for frame in packet.decode():
                    context.check()
                    counts[packet.stream.index] += 1
            if not counts or any(count == 0 for count in counts.values()):
                raise ValueError("Результат содержит пустой или нечитаемый медиапоток.")

    def camera_frames(
        self,
        url: str,
        size: tuple[int, int] | None,
        context: OperationContext,
        timeout: float = 10,
        *,
        decoder: CameraDecoder | None = None,
    ) -> Generator[VideoFrame, None, None]:
        from .camera import camera_frames

        self.capabilities()
        try:
            yield from camera_frames(url, size, context, timeout, decoder=decoder)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "camera")) from None

    def record_camera(
        self,
        url: str,
        output: BinaryIO,
        context: OperationContext,
        timeout: float = 10,
        options: RecordOptions | None = None,
    ) -> None:
        from .camera import record_camera

        self.capabilities()
        try:
            record_camera(url, output, context, timeout, options)
        except InterruptedError:
            raise
        except Exception as error:  # noqa: BLE001 — граница нативной операции
            raise MediaOperationError(self.error(error, "record")) from None


def get_media_service() -> MediaService:
    """Использовать общий DI и при автономном запуске медиаворкеров."""
    from src.di import get_container

    with _service_lock:
        container = get_container()
        if not container.is_registered(MediaService):
            container.register(MediaService, MediaService, singleton=True)
        return cast(MediaService, container.resolve(MediaService))
