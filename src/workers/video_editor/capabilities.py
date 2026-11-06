"""Отменяемая диагностика библиотек и реальная проба выбранного кодировщика."""

from __future__ import annotations
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from PySide6.QtCore import QObject, QThread, Signal
from src.domain.video.render_plan import RenderPlan, RenderCanvas
from src.services.media import get_media_service, OperationContext
from .metadata import ProbeMetadata
from .encoder_hardware import EncoderHardware

VIDEO_ENCODERS = (
    "libx264",
    "libx265",
    "h264_nvenc",
    "hevc_nvenc",
    "h264_qsv",
    "hevc_qsv",
    "h264_amf",
    "hevc_amf",
    "h264_vaapi",
    "hevc_vaapi",
)


def describe_probe_error(error: BaseException) -> str:
    """Сообщение пробы вместе с очищенным текстом FFmpeg, если он есть."""
    message = str(error)
    media = getattr(error, "error", None)
    detail = getattr(media, "detail", "")
    if detail and detail not in message:
        return f"{message} FFmpeg: {detail}"
    return message


def log_probe_error(codec: str, device: str, text: str) -> None:
    from src.logger import logger

    logger.warning(f"Проба кодировщика {codec} ({device or 'по умолчанию'}): {text}")


@dataclass(frozen=True)
class CapabilitySnapshot:
    identity: tuple[str, int, int]
    version: str
    encoders: frozenset[str]
    filters: frozenset[str]


class FFmpegCapabilities:
    @staticmethod
    def identity() -> tuple[str, int, int]:
        return get_media_service().identity, 0, 0

    @classmethod
    def inspect(cls, cancel: threading.Event | None = None) -> CapabilitySnapshot:
        OperationContext(
            cancelled=cancel if cancel is not None else threading.Event()
        ).check()
        snapshot = get_media_service().capabilities()
        return CapabilitySnapshot(
            (snapshot.identity, 0, 0),
            snapshot.version,
            snapshot.encoders,
            snapshot.filters,
        )

    @classmethod
    def limiter_filter(cls, cancel: threading.Event | None = None) -> str:
        return "alimiter=limit=0.95:level=0:latency=1"

    @classmethod
    def verify_encoder(
        cls,
        codec: str,
        cancel: threading.Event,
        device: str = "",
        *,
        force: bool = False,
    ) -> None:
        from fractions import Fraction
        from src.services.media.render_plan import RenderCommand

        snapshot = cls.inspect(cancel)
        if codec not in snapshot.encoders:
            raise ValueError("В комплекте FFmpeg отсутствует выбранный кодировщик.")
        with tempfile.TemporaryDirectory(prefix="dno-encoder-") as directory:
            output = str(Path(directory) / "probe.mkv")
            # Минимальные размеры ряда аппаратных кодировщиков больше 128×96.
            plan = RenderPlan((), 0.24, RenderCanvas(640, 360, Fraction(25)))
            graph = RenderCommand.build(
                plan, output, {}, {"vcodec": codec, "gpu_device": device}
            )
            context = OperationContext.with_timeout(12, cancel)
            try:
                get_media_service().render(graph, output, context)
                get_media_service().verify(output, context)
            except InterruptedError:
                raise
            except Exception as error:
                if EncoderHardware.is_hardware(codec):
                    raise ValueError(
                        "Реальное пробное кодирование на выбранном GPU не удалось. Проверьте устройство и драйвер. "
                        + describe_probe_error(error)
                    ) from None
                raise


class CapabilityWorker(QThread):
    snapshot_ready = Signal(object)
    encoder_checked = Signal(str, str)

    def __init__(
        self,
        codec: str = "",
        parent: QObject | None = None,
        plan: RenderPlan | None = None,
        device: str = "",
        codecs: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.codec = codec
        self.device = device
        self.codecs = codecs
        self.cancelled = threading.Event()
        self.snapshot: CapabilitySnapshot | None = None
        self.error = ""
        self.plan = plan
        self.metadata: dict[str, ProbeMetadata] = {}

    def run(self) -> None:
        try:
            self.snapshot = FFmpegCapabilities.inspect(self.cancelled)
            if self.plan is not None:
                reference = self.plan.canvas_source or (
                    self.plan.video[0] if self.plan.video else None
                )
                if reference:
                    from .ffmpeg_utils import probe_media

                    self.metadata[reference.path] = probe_media(
                        reference.path, cancelled=self.cancelled
                    )
                    if not self.metadata[reference.path]:
                        raise ValueError("Не удалось определить параметры исходника.")
            self.snapshot_ready.emit(self.snapshot)
            if self.codecs:
                hardware = EncoderHardware.detect(self.cancelled)
                for codec in self.codecs:
                    OperationContext(cancelled=self.cancelled).check()
                    if codec not in self.snapshot.encoders:
                        continue
                    if not hardware.devices_for(codec, self.device):
                        self.encoder_checked.emit(
                            codec,
                            hardware.error
                            or "Для этого кодировщика нет доступного адаптера в системе.",
                        )
                        continue
                    try:
                        FFmpegCapabilities.verify_encoder(
                            codec, self.cancelled, self.device, force=True
                        )
                    except InterruptedError:
                        raise
                    except Exception as error:  # noqa: BLE001
                        text = describe_probe_error(error)
                        log_probe_error(codec, self.device, text)
                        self.encoder_checked.emit(codec, text)
                    else:
                        self.encoder_checked.emit(codec, "")
            elif self.codec:
                FFmpegCapabilities.verify_encoder(
                    self.codec, self.cancelled, self.device
                )
        except Exception as error:  # noqa: BLE001 — граница рабочего потока
            self.error = str(error)
