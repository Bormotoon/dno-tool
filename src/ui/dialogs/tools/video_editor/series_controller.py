"""Очередь последовательного экспорта с отдельной отменой текущего и всей серии."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QMessageBox
from src.domain.video.render_plan import RenderPlan
from src.domain.video.series import SeriesTarget
from src.workers.video_editor.export_worker import ExportWorker, ExportParams


class SeriesController(QObject):
    def __init__(
        self, view, plan: RenderPlan, targets: list[SeriesTarget], overwrite: bool
    ) -> None:
        super().__init__(view)
        self.view, self.plan, self.targets, self.overwrite = (
            view,
            plan,
            targets,
            overwrite,
        )
        self.worker: ExportWorker | None = None
        self.thread = None
        self.index, self.completed, self.skipped = 0, 0, 0
        self.cancelled = False
        self.error = ""
        self.fingerprints = {
            path: ExportWorker._fingerprint(path) for path in plan.sources
        }
        self.protected_paths = list(
            dict.fromkeys(
                [asset.path for asset in view.project.assets] + list(plan.sources)
            )
        )
        self.active = True

    def start(self) -> None:
        self.view.btn_cancel.setText("Отменить текущий файл")
        self._next()

    def cancel(self, all_jobs: bool = False) -> None:
        self.cancelled = self.cancelled or all_jobs
        if self.worker:
            self.worker.cancel()

    def _next(self) -> None:
        if (
            self.view._closing
            or self.cancelled
            or self.error
            or self.index >= len(self.targets)
        ):
            self.active = False
            self.view._update_editor_actions()
            self.view._hide_progress()
            self.view.btn_cancel.setText("Отмена")
            self.view.btn_cancel.setEnabled(True)
            if not self.view._closing:
                if self.error:
                    QMessageBox.warning(self.view, "Серия остановлена", self.error)
                else:
                    QMessageBox.information(
                        self.view,
                        "Серия фрагментов",
                        f"Готово: {self.completed}. Пропущено/отменено: {self.skipped}.",
                    )
            return
        target = self.targets[self.index]
        self.index += 1
        if target.skip:
            self.skipped += 1
            QTimer.singleShot(0, self._next)
            return
        try:
            if any(
                ExportWorker._fingerprint(path) != version
                for path, version in self.fingerprints.items()
            ):
                raise ValueError(
                    "Исходник изменился между файлами серии. Проверьте монтаж и повторите экспорт."
                )
            parameters = ExportParams(
                mode="reencode",
                out_path=target.path,
                container=Path(target.path).suffix[1:],
                overwrite=self.overwrite,
                audio_only=Path(target.path).suffix.lower() not in (".mp4", ".mkv"),
                ab="192k",
            )
            worker = ExportWorker(
                self.plan.section(target.start, target.end),
                parameters,
                source_paths=self.protected_paths,
            )
            self.worker = worker
            worker.finished.connect(self._file_done)
            worker.error.connect(self._file_error)
            worker.progress.connect(self._progress)
            self.view.btn_cancel.setEnabled(True)
            self.view._show_progress(
                f"Файл {self.index}/{len(self.targets)}", cancel=True
            )
            self.view.progress.setRange(0, 100)
            worker.start(self.view)
            self.thread = worker.thread()
            self.view._update_editor_actions()
            self.thread.finished.connect(self._stopped)
        except Exception as error:
            self.error = str(error)
            QTimer.singleShot(0, self._next)

    @Slot(int, str)
    def _progress(self, percent: int, stage: str) -> None:
        self.view._on_export_progress(
            percent, f"Файл {self.index}/{len(self.targets)}: {stage}"
        )

    @Slot(str)
    def _file_done(self, path: str) -> None:
        self.completed += 1

    @Slot(str)
    def _file_error(self, message: str) -> None:
        if message == "Экспорт отменен":
            self.skipped += 1
        else:
            self.error = message

    @Slot()
    def _stopped(self) -> None:
        self.worker, self.thread = None, None
        QTimer.singleShot(0, self._next)
