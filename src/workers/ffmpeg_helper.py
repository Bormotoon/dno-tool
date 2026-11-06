"""Фоновая диагностика единого комплекта и реальное кодирование на устройствах."""

from __future__ import annotations
import threading
from PySide6.QtCore import QThread, Signal, QObject
from src.services.media import get_media_service, OperationContext
from src.workers.video_editor.capabilities import (
    FFmpegCapabilities,
    VIDEO_ENCODERS,
    describe_probe_error,
    log_probe_error,
)
from src.workers.video_editor.encoder_hardware import EncoderHardware


class MediaDiagnosticsWorker(QThread):
    report = Signal(object)
    gpu_checked = Signal(str, str, str)

    def __init__(
        self, parent: QObject | None = None, *, check_gpu: bool = False
    ) -> None:
        super().__init__(parent)
        self.cancelled = threading.Event()
        self.check_gpu = check_gpu
        self.error = ""

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            context = OperationContext(cancelled=self.cancelled)
            context.check()
            snapshot = get_media_service().capabilities()
            self.report.emit(snapshot)
            if self.check_gpu:
                hardware = EncoderHardware.detect(self.cancelled)
                candidates = [
                    (codec, "")
                    for codec in ("libx264", "libx265")
                    if codec in snapshot.encoders
                ]
                candidates.extend(
                    (codec, device)
                    for codec in VIDEO_ENCODERS
                    if EncoderHardware.is_hardware(codec)
                    and codec in snapshot.encoders
                    for device in hardware.devices_for(codec, None)
                )
                for codec, device in candidates:
                    context.check()
                    error = ""
                    try:
                        FFmpegCapabilities.verify_encoder(
                            codec, self.cancelled, device, force=True
                        )
                    except InterruptedError:
                        raise
                    except Exception as problem:
                        error = describe_probe_error(problem)
                        log_probe_error(codec, device, error)
                    self.gpu_checked.emit(codec, device or "По умолчанию", error)
                if not any(EncoderHardware.is_hardware(codec) for codec, _ in candidates):
                    self.gpu_checked.emit(
                        "GPU",
                        "",
                        hardware.error
                        or "Нет доступных GPU-кодировщиков из поддерживаемого приложением списка.",
                    )
        except InterruptedError:
            self.error = "Проверка отменена."
        except Exception as error:
            self.error = get_media_service().error(error, "diagnostics").message
