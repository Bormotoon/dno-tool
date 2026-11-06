"""Отменяемое измерение пика исходного звукового фрагмента."""

from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6.QtCore import QThread

from src.domain.video.render_plan import RenderClip
from .ffmpeg_utils import probe_media
from src.services.media import get_media_service, OperationContext


class AudioAnalysisWorker(QThread):
    def __init__(self, clip: RenderClip, parent=None) -> None:
        super().__init__(parent)
        self.clip = clip
        self.cancelled = threading.Event()
        self.gain: float | None = None
        self.error = ""
        self.peak_db = 0.0

    def run(self) -> None:
        try:
            self.gain = self.measure()
        except Exception as error:
            self.error = "Измерение отменено" if self.cancelled.is_set() else str(error)

    def measure(self) -> float:
        control = OperationContext.with_timeout(300, self.cancelled)
        control.check()
        path = Path(self.clip.path)
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        metadata = probe_media(str(path), cancelled=self.cancelled)
        streams = metadata.get("audio_streams", [])
        if not 0 <= self.clip.stream < len(streams):
            raise ValueError("Выбранный аудиопоток отсутствует.")
        origin = (
            float(metadata.get("vstart", metadata.get("start_time", 0)))
            if metadata.get("has_video")
            else float(metadata.get("start_time", 0))
        )
        offset = (
            float(
                streams[self.clip.stream].get("start_time", metadata.get("astart", 0))
                or 0
            )
            - origin
        )
        start, end = (
            max(0, self.clip.src_in - offset),
            max(0, self.clip.src_out - offset),
        )
        peak = get_media_service().peak(
            str(path), self.clip.stream, start, end, control
        )
        if not math.isfinite(peak) or peak <= -90:
            raise ValueError("Фрагмент содержит тишину; усиление не изменено.")
        if before != (path.stat().st_size, path.stat().st_mtime_ns):
            raise ValueError("Исходник изменился во время измерения.")
        self.peak_db = peak
        return min(100.0, 10 ** ((-1 - peak) / 20))
