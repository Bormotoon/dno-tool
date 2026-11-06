"""Ограниченный производный диапазон для воспроизведения общего плана."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal

from src.domain.video.failures import error_detail
from src.domain.video.render_plan import RenderPlan
from src.logger import logger
from .export_worker import ExportParams, ExportWorker


class PreviewRenderWorker(QThread):
    progress = Signal(int)

    def __init__(
        self,
        plan: RenderPlan,
        text: str | None,
        start: float,
        end: float,
        generation: int,
        retired: list[tempfile.TemporaryDirectory],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.text = text
        self.start_time = start
        self.end_time = end
        self.generation = generation
        self.retired = retired
        self.cancelled = threading.Event()
        self.directory: tempfile.TemporaryDirectory | None = None
        self.output = ""
        self.error = ""
        self.fps = 25.0
        self.size = (0, 0)
        self._percent = -1

    def _report(self, percent: int, _stage: str) -> None:
        if percent != self._percent:
            self._percent = percent
            self.progress.emit(percent)

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        from .render_command import RenderCommand

        try:
            for directory in list(self.retired):
                try:
                    directory.cleanup()
                    self.retired.remove(directory)
                except OSError:
                    pass
            if self.cancelled.is_set():
                return
            section = self.plan.section(self.start_time, self.end_time)
            self.directory = tempfile.TemporaryDirectory(prefix="dno-preview-")
            self.output = str(Path(self.directory.name) / "preview.mp4")
            worker = ExportWorker(
                section,
                ExportParams(
                    mode="reencode",
                    out_path=self.output,
                    drawtext=self.text,
                    vb="2000k",
                    ab="192k",
                    # Временный файл живёт до следующей правки: скорость важнее
                    # размера, а длительность результата проверяется всё равно.
                    preset="ultrafast",
                    verify_decode=False,
                ),
            )
            worker._cancel_event = self.cancelled
            worker._control.cancelled = self.cancelled
            errors: list[str] = []
            worker.error.connect(errors.append)
            worker.progress.connect(self._report, Qt.ConnectionType.DirectConnection)
            worker._run()
            if errors:
                detail = worker.failure.detail if worker.failure else ""
                raise RuntimeError(f"{errors[0]} {detail}".strip())
            if self.cancelled.is_set():
                return
            width, height, fps = RenderCommand.canvas(section, worker._metadata, {})
            self.size = (width, height)
            self.fps = float(fps)
            if Path(self.output).stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Диапазон preview превысил бюджет 64 МиБ.")
        except Exception as error:
            self.error = str(error)
            logger.error(f"Предпросмотр: {error_detail(error)}")
        finally:
            if self.directory is not None and (self.cancelled.is_set() or self.error):
                self.directory.cleanup()
                self.directory = None
