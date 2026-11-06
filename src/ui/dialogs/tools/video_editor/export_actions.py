"""Запуск экспорта, полоса прогресса и разбор его ошибок."""

from __future__ import annotations


from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from src.domain.video.render_plan import RenderPlan
from src.logger import logger
from src.workers.video_editor.export_worker import ExportParams, ExportWorker
from src.workers.video_editor.metadata import ProbeMetadata

from .export_dialog import ExportDialog


class VideoExportMixin:
    """Часть VideoEditorDialog: состояние и виджеты принадлежат диалогу."""

    def _timeline_segments(self) -> list[tuple[str, float, float, float, bool]]:
        """Сегменты экспорта: (path, src_in, src_out, vol, muted).

        Громкость - с V-клипа (пара синхронизирована через _sync_mate_audio):
        mute дает vol 0.0.
        """
        # Порядок строго по старту на таймлайне
        ordered: list[tuple[float, str, float, float, float, bool]] = []
        for tr in self.project.video_tracks():
            for c in tr.sorted_clips():
                a = self._assets.get(c.asset_iid)
                if a:
                    vol = 0.0 if c.mute else max(0.0, min(2.0, float(c.volume)))
                    ordered.append(
                        (
                            c.timeline_start,
                            a.path,
                            c.src_in,
                            c.src_out,
                            vol,
                            bool(c.mute),
                        )
                    )
        ordered.sort()
        return [(p, i, o, v, m) for _, p, i, o, v, m in ordered]

    def do_export(self) -> None:
        self._finish_edit_sessions()
        if self._project_can_start_io():
            self._start_project_io(
                "preflight", "", self._take_snapshot(), self._do_export_ready
            )

    def _do_export_ready(self) -> None:
        if self._closing:
            return
        if self._repair_in_progress:
            QMessageBox.warning(
                self, "Экспорт", "Дождитесь завершения ремонта контейнера."
            )
            return
        from src.domain.video.render_plan import RenderPlan

        try:
            plan = RenderPlan.from_project(self.project)
        except ValueError as error:
            QMessageBox.warning(self, "Экспорт", str(error))
            return
        segs = self._timeline_segments()
        if not plan.clips:
            QMessageBox.warning(self, "Экспорт", "Таймлайн пуст.")
            return
        metas: list[ProbeMetadata] = []
        for p, _i, _o, _v, _m in segs:
            a = next((x for x in self._assets.values() if x.path == p), None)
            if a:
                metas.append(
                    ProbeMetadata(
                        path=a.path,
                        vcodec=a.vcodec,
                        width=a.width,
                        height=a.height,
                        has_audio=a.has_audio,
                        copy_signature=a.copy_signature,
                    )
                )
        compat_ok, compat_msg = ExportWorker.check_compat(metas)
        reasons = self.project.copy_reasons()
        if reasons:
            compat_ok = False
            compat_msg = "; ".join(reasons)
        has_text = self._has_visible_text()
        selected = self._selected_clip_obj()
        dlg = ExportDialog(
            self,
            has_text=has_text,
            compat_ok=compat_ok,
            compat_msg=compat_msg,
            copy_sources=tuple(dict.fromkeys(segment[0] for segment in segs)),
            duration=plan.duration,
            mark_range=(
                (self._mark_in, self._mark_out)
                if self._mark_in is not None and self._mark_out is not None
                else None
            ),
            clip_range=(
                (selected[0].timeline_start, selected[0].timeline_end)
                if selected
                else None
            ),
            position=self._playhead,
            audio_project=not bool(plan.video),
            profile_index=(
                self._editor_view_state.get("video_editor", "export_profile")
                if self._editor_view_state
                else None
            ),
            plan=plan,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if self._editor_view_state:
            self._editor_view_state.set(
                "video_editor",
                "export_profile",
                v.get("profile", 0),
                track_change=False,
            )
        if not v["out"]:
            QMessageBox.warning(self, "Экспорт", "Укажите файл результата.")
            return
        mode = v["mode"]
        if v.get("range") is not None:
            plan = plan.section(*v["range"])
        # Copy физически не меняет звук: нестандартная громкость клипов
        # там потеряется - предложить перекодирование
        if mode == "copy" and any(s[3] != 1.0 or s[4] for s in segs):
            r = QMessageBox.warning(
                self,
                "Экспорт",
                "На таймлайне есть клипы с измененной громкостью или Mute - "
                "Copy-режим их не применит (звук меняется только при "
                "перекодировании). Переключиться на перекодирование?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
            mode = "reencode"
        if mode == "copy" and has_text:
            r = QMessageBox.warning(
                self,
                "Экспорт",
                "Текст в углу требует перекодирования и будет пропущен "
                "в Copy-режиме. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        params = ExportParams(
            mode=mode,
            out_path=v["out"],
            master_volume=self.project.effective_master_volume,
            container=v["format"],
            overwrite=v["overwrite"],
            audio_only=v.get("audio_only", False),
            crf=v.get("crf", 23),
            preset=v.get("preset", "medium"),
        )
        if mode == "reencode":
            params.vcodec = v["vcodec"]
            params.gpu_device = v.get("gpu_device", "")
            params.width = v["width"]
            params.height = v["height"]
            params.fps = v["fps"]
            params.vb = v["vb"]
            params.ab = v["ab"]
        else:
            # Copy: расширить out расширением формата не нужно, путь как есть
            pass
        self._run_export(plan, params)

    def _show_progress(self, text: str, cancel: bool = True) -> None:
        """Полоса прогресса + кнопка Отмена (прижата вправо)."""
        self.progress.setFormat(text)
        self.btn_cancel.setVisible(cancel)
        parent = self.progress.parentWidget()
        if parent is not None:
            parent.show()
        self.progress.show()

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.btn_cancel.hide()
        parent = self.progress.parentWidget()
        if parent is not None:
            parent.hide()

    def _cancel_operation(self) -> None:
        """Кнопка Отмена: прервать ремонт или экспорт."""
        self.btn_cancel.setEnabled(False)
        if self._series is not None and self._series.active:
            self._series.cancel()
        elif self._repair_in_progress:
            self._op_cancel.set()
        elif self._audio_analysis is not None:
            self._audio_analysis.cancelled.set()
        elif self._export_worker is not None:
            self._op_cancel.set()
            try:
                self._export_worker.cancel()
            except Exception as e:
                logger.error(f"cancel export: {e}")

    def _run_export(
        self,
        segs: RenderPlan | list[tuple[str, float, float, float, bool]],
        params: ExportParams,
    ) -> None:
        if self._closing:
            return
        if self._export_worker is not None:
            if self._thread_is_running(self._export_thread):
                self._on_export_progress(0, "Предыдущий экспорт ещё завершается")
                return
            self._export_worker = None
            self._export_thread = None
        self._op_cancel.clear()
        self._show_progress("Экспорт...", cancel=True)
        self.progress.setValue(0)
        self.progress.setRange(0, 100)
        self.panel.set_export_enabled(False)
        w = ExportWorker(
            segs, params, source_paths=[asset.path for asset in self.project.assets]
        )
        w.progress.connect(self._on_export_progress)
        w.finished.connect(self._on_export_done)
        w.error.connect(self._on_export_error)
        self._export_worker = w
        # Родитель потока - диалог: GC не удалит живой QThread.
        # Ссылку сбрасываем только по finished потока.
        w.start(self)
        th = w.thread()
        self._export_thread = th
        self._update_editor_actions()
        if th is not None:
            try:
                th.finished.connect(
                    self._on_export_thread_stopped, Qt.ConnectionType.QueuedConnection
                )
            except Exception as e:
                logger.error(f"export thread hook: {e}")

    def _on_export_progress(self, pct: int, stage: str) -> None:
        if self._closing:
            return
        try:
            self.progress.setValue(max(0, min(100, int(pct))))
            self.progress.setFormat(f"{stage} %p%" if stage else "%p%")
        except RuntimeError:
            pass

    def _on_export_thread_finished(self, w: ExportWorker | None) -> None:
        if w is not None and self._export_worker is w:
            try:
                thread = self._export_thread or w.thread()
            except RuntimeError:
                thread = None
            if self._thread_is_running(thread):
                return
            self._export_worker = None
            self._export_thread = None

    @Slot()
    def _on_export_thread_stopped(self) -> None:
        if self._export_worker is not None:
            self._on_export_thread_finished(self._export_worker)
            if self._export_worker is not None:
                QTimer.singleShot(10, self._on_export_thread_stopped)
        self._update_editor_actions()

    def _on_export_done(self, path: str) -> None:
        # Ссылку НЕ сбрасываем здесь: поток еще гасит цикл, сброс -
        # в _on_export_thread_finished, иначе GC убьет живой QThread.
        if self._closing:
            return
        self._hide_progress()
        self.btn_cancel.setEnabled(True)
        self.panel.set_export_enabled(True)
        QMessageBox.information(self, "Экспорт", f"Готово: {path}")

    def _on_export_error(self, err: str) -> None:
        if self._closing:
            return
        from src.domain.video.failures import VideoFailure

        failure = (
            self._export_worker.failure if self._export_worker is not None else None
        )
        failure = failure or VideoFailure.classify(err)
        self._last_export_report = (
            self._export_worker.diagnostic_report()
            if self._export_worker is not None
            else failure.report({})
        )
        logger.error(
            f"export: {failure.category}, этап {failure.stage}: {failure.detail}"
        )
        self._hide_progress()
        self.btn_cancel.setEnabled(True)
        self.panel.set_export_enabled(True)
        if err == "Экспорт отменен":
            return
        detail = f"\n\nFFmpeg: {failure.detail}" if failure.detail != err else ""
        if not failure.detail:
            detail = ""
        QMessageBox.critical(
            self,
            "Экспорт",
            f"{failure.title}\n\n{failure.action}\n\n{err[:1800]}{detail}",
        )
