"""Фоновая проверка возможности сохранить выбранные потоки без перекодирования."""

from __future__ import annotations

from .metadata import ProbeMetadata

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread

from .ffmpeg_utils import probe_media
from src.services.media import get_media_service, OperationContext


def check_copy_container(
    metadata: list[ProbeMetadata], container: str, cancelled: threading.Event
) -> None:
    """Проверить заголовки реальным muxer в ограниченном временном файле."""
    if container not in ("mp4", "mkv"):
        raise ValueError("Неизвестный контейнер Copy.")
    control = OperationContext.with_timeout(8, cancelled)
    for meta in metadata:
        control.check()
        counts = meta.get("stream_counts")
        if counts != {
            "video": 1,
            "audio": int(bool(meta.get("has_audio"))),
            "subtitle": 0,
            "data": 0,
            "attachment": 0,
        }:
            raise ValueError(
                "Copy поддерживает один видеопоток и не более одного аудиопотока. "
                "Дополнительные потоки требуют явного выбора и перекодирования."
            )
        with tempfile.TemporaryDirectory(prefix="dno-copy-check-") as directory:
            get_media_service().remux(
                [(meta["path"], 0.0, 0.1)],
                str(Path(directory) / ("check." + container)),
                control,
                max_bytes=1048576,
            )


class CopyPreflightWorker(QThread):
    def __init__(
        self, sources: tuple[str, ...], container: str, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.sources, self.container = sources, container
        self.cancelled = threading.Event()
        self.error = ""

    def run(self) -> None:
        from .export_worker import ExportWorker

        try:
            metadata = [
                probe_media(path, cancelled=self.cancelled) for path in self.sources
            ]
            compatible, reason = ExportWorker.check_compat(metadata)
            if not compatible:
                raise ValueError(reason)
            check_copy_container(metadata, self.container, self.cancelled)
        except InterruptedError:
            self.error = "Проверка Copy отменена."
        except Exception as error:
            self.error = str(error)
