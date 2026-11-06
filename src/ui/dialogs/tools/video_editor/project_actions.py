"""Контроллер файлов проекта и решений о несохранённых изменениях."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.workers.video_editor.audio_analysis import AudioAnalysisWorker

    from .series_controller import SeriesController
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QMenu,
    QMenuBar,
    QMessageBox,
)

from src.domain.video.models import Project
from src.workers.video_editor.project_file_worker import ProjectFileWorker
from src.workers.video_editor.thumbnail_worker import ThumbnailPool

from .media_bridge import MediaResultBridge


class VideoProjectMixin:
    """Файловые действия; диск обслуживает ProjectFileWorker."""

    project: Project
    _sel_clip: str | None
    _sel_track: str | None
    _mark_in: float | None
    _mark_out: float | None
    _project_recovery_source: str | None
    _recovery_restored_path: str | None
    _audio_analysis: AudioAnalysisWorker | None
    _series: SeriesController | None

    def _init_project_state(self) -> None:
        self._project_path: str | None = None
        self._project_clean_state = self.project.edit_state()
        self._project_io_thread: ProjectFileWorker | None = None
        self._project_io_result: dict | None = None
        self._project_io_error = ""
        self._project_after_io: Callable[[], None] | None = None
        self._project_close_approved = False
        self._media_generation = 0
        self._thumb_bridge: MediaResultBridge | None = None

    def _build_project_menu(self) -> QMenuBar:
        bar = QMenuBar(self)
        bar.setNativeMenuBar(False)
        menu = bar.addMenu("Проект")
        for text, shortcut, callback in (
            ("Новый", "Ctrl+N", self.new_project),
            ("Открыть…", "Ctrl+O", self.open_project),
            ("Сохранить проект", "Ctrl+S", self.save_project),
            ("Сохранить проект как…", "Ctrl+Shift+S", self.save_project_as),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(callback)
            menu.addAction(action)
        menu.addSeparator()
        history = menu.addMenu("История")
        self._undo_action = history.addAction("Отменить")
        self._redo_action = history.addAction("Повторить")
        self._undo_action.triggered.connect(self.undo_edit)
        self._redo_action.triggered.connect(self.redo_edit)
        menu.addSeparator()
        self._recovery_action = menu.addAction("Восстановить после сбоя…")
        self._recovery_action.setEnabled(False)
        self._recovery_action.triggered.connect(self.offer_recovery)
        menu.addSeparator()
        from .advanced_controller import AdvancedController

        self._advanced = AdvancedController(self)

        def add_actions(
            target: QMenu, entries: tuple[tuple[str, Callable[[], None]], ...]
        ) -> None:
            for title, callback in entries:
                target.addAction(title).triggered.connect(callback)

        add_actions(
            menu.addMenu("Исходники"),
            (
                ("Заменить исходник…", self.relink_source),
                ("Перепривязать папку исходников…", self.relink_source_folder),
            ),
        )
        add_actions(
            menu.addMenu("Параметры проекта"),
            (
                ("Холст и FPS…", self.edit_project_canvas),
                ("Маркеры и фрагменты…", self.edit_markers),
            ),
        )
        add_actions(
            menu.addMenu("Экспорт"),
            (
                ("Экспорт серии фрагментов…", self.export_series),
                ("Отменить серию", self.cancel_export_series),
                ("Раскадровка монтажа…", self._advanced.storyboard),
            ),
        )

        from .edit_actions import EditActions

        self.edit_actions = EditActions(self)
        self.edit_actions.build_menu(bar.addMenu("Монтаж"))
        self._recovery_label = QLabel(self)
        bar.setCornerWidget(self._recovery_label)
        self._update_project_title()
        view = bar.addMenu("Вид")
        from .monitor_workspace import MonitorWorkspace

        monitors_menu = view.addMenu("Просмотры")
        self._monitor_actions: dict[str, QAction] = {}
        self._monitor_action_group = QActionGroup(self)
        self._monitor_action_group.setExclusive(True)
        for mode, title in MonitorWorkspace.MODES.items():
            action = monitors_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(mode == "dual")
            self._monitor_action_group.addAction(action)
            action.triggered.connect(
                lambda checked=False, key=mode: self.monitors.set_mode(key)
            )
            self._monitor_actions[mode] = action
        self._show_media_action = view.addAction("Медиапанель")
        self._show_text_action = view.addAction("Инспектор")
        for action in (self._show_media_action, self._show_text_action):
            action.setCheckable(True)
            action.setChecked(True)
        self._show_media_action.toggled.connect(
            lambda visible: self.panel.setVisible(visible)
        )
        self._show_text_action.toggled.connect(
            lambda visible: self._text_column.setVisible(visible)
        )
        help_menu = bar.addMenu("Справка")
        help_menu.addAction("Клавиши").triggered.connect(self.show_editor_keys)
        diagnostic = help_menu.addAction("Диагностика экспорта")
        diagnostic.triggered.connect(self.show_export_diagnostic)
        return bar

    def show_export_diagnostic(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Диагностика экспорта")
        box.setText(
            "Отчёт не содержит путей, изображения, текста титров и полного filtergraph."
        )
        box.setDetailedText(
            self._last_export_report or "Ошибок экспорта в этом окне ещё не было."
        )
        box.exec()

    def edit_clip_properties(self) -> None:
        self.inspector_controller.open("bounds")

    def edit_markers(self) -> None:
        from PySide6.QtWidgets import QDialog

        from .marker_dialog import MarkerDialog

        self._finish_edit_sessions()
        dialog = MarkerDialog(
            self.project.markers,
            self.project.duration(),
            self._playhead,
            (
                (self._mark_in, self._mark_out)
                if self._mark_in is not None and self._mark_out is not None
                else None
            ),
            self,
        )
        dialog.seek.connect(self._on_timeline_seek)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            before = self._take_snapshot()
            self.edits.markers(dialog.values())
            self.push_undo(before, "Маркеры проекта")

    def export_series(self) -> None:
        self._finish_edit_sessions()
        if self._project_can_start_io():
            self._start_project_io(
                "preflight", "", self._take_snapshot(), self._export_series_ready
            )

    def _export_series_ready(self) -> None:
        from PySide6.QtWidgets import QDialog

        from src.domain.video.render_plan import RenderPlan

        from .series_controller import SeriesController
        from .series_dialog import SeriesDialog

        if not any(marker.end > marker.start for marker in self.project.markers):
            QMessageBox.information(
                self,
                "Серия",
                "Добавьте именованные диапазоны в «Проект → Маркеры и фрагменты».",
            )
            return
        dialog = SeriesDialog(self.project.markers, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            plan = RenderPlan.from_project(self.project)
            self._series = SeriesController(
                self, plan, dialog.targets, dialog.overwrite.isChecked()
            )
            self._series.start()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Серия", str(error))

    def cancel_export_series(self) -> None:
        if self._series is not None:
            self._series.cancel(all_jobs=True)

    def edit_clip_audio(self) -> None:
        self.inspector_controller.open("audio")

    def normalize_clip_audio(self) -> None:
        from src.domain.video.render_plan import RenderPlan
        from src.workers.video_editor.audio_analysis import AudioAnalysisWorker

        self._finish_edit_sessions()
        if not self._project_can_start_io():
            return
        if self._audio_analysis is not None:
            return
        found = self._selected_clip_obj()
        if found is None:
            return
        clip, track = found
        if track.kind != "audio":
            clip = next(
                (
                    other
                    for audio in self.project.audio_tracks()
                    for other in audio.clips
                    if clip.link_iid and other.link_iid == clip.link_iid
                ),
                None,
            )
        if clip is None:
            return
        plan = RenderPlan.from_project(self.project)
        render_clip = next(item for item in plan.clips if item.iid == clip.iid)
        worker = AudioAnalysisWorker(render_clip, self)
        self._audio_analysis = worker
        self._audio_analysis_state = (self.project.iid, clip.to_dict())
        worker.finished.connect(self._normalized_clip_audio)
        worker.start()
        self._show_progress("Измерение пика звука…", cancel=True)
        self.progress.setRange(0, 0)
        self._update_editor_actions()

    def crossfade_selected_audio(self) -> None:
        found = self._selected_clip_obj()
        if found and found[1].kind != "audio":
            found = next(
                (
                    (c, tr)
                    for tr in self.project.audio_tracks()
                    for c in tr.clips
                    if found[0].link_iid and c.link_iid == found[0].link_iid
                ),
                None,
            )
        if found is None:
            QMessageBox.information(
                self, "Кроссфейд", "Выберите звуковой клип перед стыком."
            )
            return
        duration, accepted = QInputDialog.getDouble(
            self,
            "Кроссфейд",
            "Длительность, секунды (нужны запасы исходников):",
            1,
            0.001,
            600,
            3,
        )
        if not accepted:
            return
        self._finish_edit_sessions()
        before = self._take_snapshot()
        try:
            self.project.crossfade_audio(found[0].iid, duration)
        except ValueError as error:
            QMessageBox.warning(self, "Кроссфейд", str(error))
            return
        self.push_undo(before, "Кроссфейд звука")

    def add_audio_track(self) -> None:

        if len(self.project.audio_tracks()) >= 64:
            QMessageBox.information(
                self, "Дорожки", "Достигнут лимит 64 звуковых дорожек."
            )
            return
        name, accepted = QInputDialog.getText(
            self,
            "Аудиодорожка",
            "Название (например, Микрофон или Музыка):",
            text=f"A{len(self.project.audio_tracks()) + 1}",
        )
        if accepted and name.strip():
            self._finish_edit_sessions()
            before = self._take_snapshot()
            self.project.add_track("audio", name.strip()[:100])
            self.push_undo(before, "Добавить аудиодорожку")

    def _normalized_clip_audio(self) -> None:
        worker, self._audio_analysis = self._audio_analysis, None
        worker.deleteLater()
        self._hide_progress()
        self.btn_cancel.setEnabled(True)
        self._update_editor_actions()
        if self._closing or worker.cancelled.is_set():
            return
        if worker.error:
            QMessageBox.warning(self, "Нормализация", worker.error)
            return
        project_iid, previous = self._audio_analysis_state
        clip = next(
            (
                clip
                for track in self.project.audio_tracks()
                for clip in track.clips
                if clip.iid == worker.clip.iid
            ),
            None,
        )
        if (
            project_iid != self.project.iid
            or clip is None
            or clip.to_dict() != previous
        ):
            QMessageBox.information(
                self,
                "Нормализация",
                "Клип изменился во время измерения. Повторите нормализацию.",
            )
            return
        before = self._take_snapshot()
        try:
            self.edits.properties("clip", clip.iid, {"normalization_gain": worker.gain})
        except ValueError as error:
            QMessageBox.warning(self, "Нормализация", str(error))
            return
        self.push_undo(before, "Нормализовать пик звука")
        self.panel.summary.setText(
            f"Исходный пик: {worker.peak_db:.1f} dBFS; коэффициент ×{worker.gain:.3f} (максимум ×100). Громкость клипа и мастер применяются дополнительно."
        )

    def unlink_selected_clip(self) -> None:
        if not self._sel_clip:
            return
        before = self._take_snapshot()
        try:
            changed = self.project.unlink_clip(self._sel_clip)
        except ValueError as error:
            QMessageBox.warning(self, "Видео и звук", str(error))
            return
        if changed:
            self.push_undo(before, "Разделить видео и аудио")

    def show_editor_keys(self) -> None:
        QMessageBox.information(
            self,
            "Клавиши видеоредактора",
            "Ctrl+N/O/S — новый / открыть / сохранить проект\n"
            "Ctrl+Shift+S — сохранить как\nCtrl+Z / Ctrl+Y — отменить / повторить\n"
            "Space — воспроизведение / пауза\nS — разрезать все медиаклипы под курсором\n"
            "I / O — границы диапазона\nCtrl+Shift+U — разделить видео и аудио\n"
            "A / Ф — выделение; B / И — лезвие; Escape — выделение\n"
            "N / Т — привязка\nDelete — удалить с промежутком\n"
            "R / Shift+R — повернуть клип на 90° по / против часовой стрелки\n"
            "H / V — отразить клип по горизонтали / вертикали\n"
            "Shift+Delete — удалить со сдвигом на общей временной оси\n"
            "Left / Right — шаг по кадрам проекта\nUp / Down — предыдущий / следующий стык\n"
            "Home / End — начало / конец проекта\nCtrl+колесо — масштаб у курсора\n"
            "Alt при перетаскивании — временно отключить привязку\n"
            "Таймкод: секунды, ЧЧ:ММ:СС.мс или ЧЧ:ММ:СС:кадры\n\n"
            "Текст на T-дорожке: перенос блока меняет время, его края — начало и конец. "
            "У краёв видео, звука и текста курсор меняется на горизонтальный; "
            "связанная V/A-пара обрезается вместе, отвязанные части — независимо. "
            "ПКМ по медиаклипу: разделить пару, удалить только звук или только видео. "
            "Эти удаления сохраняют положение второй части.\n\n"
            "Шаг соответствует CFR проекта; для исходного VFR — приближение. "
            "В полях монтажные клавиши отключены, Ctrl+Z относится к вводу. "
            "История: до 100 шагов и 32 МиБ сжатых снимков; старые шаги вытесняются.",
        )

    def edit_project_canvas(self) -> None:
        from PySide6.QtWidgets import QDialog

        from .canvas_dialog import CanvasDialog

        self._finish_edit_sessions()
        dialog = CanvasDialog(self.project.canvas, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            before = self._take_snapshot()
            self.edits.canvas(dialog.values())
            self.push_undo(before, "Холст проекта")

    def _project_edit_state(self) -> dict:
        state = self.project.edit_state()
        if self._text_before is not None:
            state["text_overlays"] = [
                overlay.to_dict() for overlay in self.text_panel.get_all()
            ]
        return state

    def is_project_dirty(self) -> bool:
        return self._project_edit_state() != self._project_clean_state

    def _update_project_title(self) -> None:
        title = (
            Path(self._project_path).name if self._project_path else self.project.name
        )
        self.setWindowTitle(f"Видеоредактор — {title}[*]")
        dirty = self.is_project_dirty()
        self.setWindowModified(dirty)
        self._schedule_recovery(dirty)

    def _project_can_start_io(self) -> bool:
        if self._closing or self._project_io_thread is not None:
            return False
        if (
            self._thread_is_running(self._export_thread)
            or self._repair_in_progress
            or self._audio_analysis is not None
            or (self._series is not None and self._series.active)
        ):
            QMessageBox.information(
                self, "Проект", "Дождитесь завершения экспорта или ремонта файла."
            )
            return False
        return True

    def _guard_project_action(self, action: Callable[[], None]) -> bool | None:
        if self._project_io_thread is not None:
            return False
        self._finish_edit_sessions()
        if self.is_project_dirty():
            choice = QMessageBox.warning(
                self,
                "Несохранённый проект",
                "Сохранить изменения монтажа перед продолжением?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Save:
                return None if self._save_project(after_success=action) else False
            if choice != QMessageBox.StandardButton.Discard:
                return False
        action()
        return True

    @Slot()
    def new_project(self) -> None:
        if self._project_can_start_io():
            self._guard_project_action(
                lambda: self._install_project(Project().to_dict(), None)
            )

    @Slot()
    def open_project(self) -> None:
        if not self._project_can_start_io():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть проект",
            self._project_path or "",
            "Проект DNO (*.dnovideo *.json)",
        )
        if path:
            self._guard_project_action(lambda: self._start_project_io("load", path))

    @Slot()
    def save_project(self) -> None:
        self._save_project()

    @Slot()
    def save_project_as(self) -> None:
        self._save_project(force_path=True)

    def _save_project(
        self, force_path: bool = False, after_success: Callable[[], None] | None = None
    ) -> bool:
        if not self._project_can_start_io():
            return False
        self._finish_edit_sessions()
        path = self._project_path
        overwrite = not (force_path or not path)
        if force_path or not path:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Сохранить проект",
                path or "project.dnovideo",
                "Проект DNO (*.dnovideo);;JSON (*.json)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
            if not path:
                return False
            if not Path(path).suffix:
                path += ".dnovideo"
            if Path(path).suffix.lower() not in (".dnovideo", ".json"):
                QMessageBox.warning(
                    self, "Проект", "Используйте расширение .dnovideo или .json."
                )
                return False
            if Path(path).exists():
                answer = QMessageBox.question(
                    self,
                    "Заменить проект?",
                    f"Файл «{Path(path).name}» уже существует. Заменить его?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                overwrite = True
        self._start_project_io(
            "save", path, self._take_snapshot(), after_success, overwrite=overwrite
        )
        return True

    def _start_project_io(
        self,
        operation: str,
        path: str,
        snapshot: dict | None = None,
        after_success: Callable[[], None] | None = None,
        replacements: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> None:
        if self._project_io_thread is not None:
            return
        self._project_io_result = None
        self._project_io_error = ""
        self._project_after_io = after_success
        worker = ProjectFileWorker(
            operation, path, snapshot, self, replacements, overwrite
        )
        self._project_io_thread = worker
        worker.result.connect(self._on_project_io_result)
        worker.error.connect(self._on_project_io_error)
        worker.finished.connect(self._on_project_io_stopped)
        worker.finished.connect(worker.deleteLater)
        labels = {
            "save": "Сохранение проекта…",
            "load": "Открытие проекта…",
            "relink": "Проверка новых исходников…",
            "preflight": "Проверка исходников перед экспортом…",
        }
        self._show_progress(labels[operation], cancel=False)
        self.progress.setRange(0, 0)
        self.setEnabled(False)
        worker.start()

    @Slot(object)
    def _on_project_io_result(self, result: dict) -> None:
        self._project_io_result = result

    @Slot(str)
    def _on_project_io_error(self, error: str) -> None:
        self._project_io_error = error

    @Slot()
    def _on_project_io_stopped(self) -> None:
        self._project_io_thread = None
        self.setEnabled(True)
        self._hide_progress()
        QTimer.singleShot(0, self._next_repair_offer)
        result, self._project_io_result = self._project_io_result, None
        after, self._project_after_io = self._project_after_io, None
        if self._project_io_error or result is None:
            QMessageBox.critical(
                self,
                "Проект",
                self._project_io_error or "Операция с проектом не завершена.",
            )
            self._update_project_title()
            if after is not None:
                self.close_cancelled.emit()
            return
        if result["operation"] == "preflight":
            if result["missing"]:
                self._mark_missing_media(result["missing"])
                QMessageBox.warning(
                    self,
                    "Экспорт недоступен",
                    "Не найдены используемые исходники:\n"
                    + "\n".join(result["missing"][:30])
                    + "\nПерепривяжите медиа через меню «Проект», картинки — в панели наложений.",
                )
            elif after is not None:
                after()
            return
        if result["operation"] == "relink":
            before = self._take_snapshot()
            self._reset_project_media()
            self.project = Project.from_dict(result["snapshot"])
            self.push_undo(before, "Перепривязать исходники")
        elif result["operation"] == "save":
            self._project_path = str(Path(result["path"]).absolute())
            self._project_clean_state = Project.from_dict(
                result["snapshot"]
            ).edit_state()
            self._update_project_title()
        else:
            recovered = self._project_recovery_source == result["path"]
            self._install_project(
                result["snapshot"],
                None if recovered else str(Path(result["path"]).absolute()),
            )
            if recovered:
                self._recovery_restored_path = self._project_recovery_source
                self._project_clean_state = None
                self._project_recovery_source = None
                self._update_project_title()
            if result["missing"]:
                self._mark_missing_media(result["missing"])
                QMessageBox.warning(
                    self,
                    "Исходники не найдены",
                    "Монтаж открыт. Недоступны исходники:\n"
                    + "\n".join(result["missing"][:30]),
                )
        if after is not None:
            self._guard_project_action(after)

    def _mark_missing_media(self, paths: list[str]) -> None:
        for asset in self.project.assets:
            if asset.path in paths:
                asset.probe_error = "Исходник недоступен. Перепривяжите файл."
                self._probe_errors[(asset.iid, asset.path)] = asset.probe_error
                self.panel.update_asset(asset.iid)

    def _install_project(self, snapshot: dict, path: str | None) -> None:
        """Сменить живой проект только после успешного чтения и валидации."""
        project = Project.from_dict(snapshot)
        self.preview.stop()
        self._reset_project_media()
        self._repair_queue.clear()
        self._repair_failed.clear()
        self._sel_clip = self._sel_track = self._sel_text = None
        self.edit_actions.set_tool("select")
        self._mark_in = self._mark_out = None
        self._playhead = 0.0
        self.project = project
        self._project_path = path
        self._project_clean_state = project.edit_state()
        self.undo_stack.clear()
        self.preview.set_marks_text(None, None)
        self.timeline.set_marks(None, None)
        self._on_project_restored()
        self._update_project_title()

    def _reset_project_media(self) -> None:
        """Смена владельца фоновых запросов отбрасывает старые результаты."""
        self._probe_requests.clear()
        self._probe_latest.clear()
        self._probe_cache.clear()
        self._probe_errors.clear()
        self._thumb_pool.signals.ready.disconnect(self._thumb_bridge.ready)
        self._thumb_pool.signals.failed.disconnect(self._thumb_bridge.failed)
        self._thumb_pool.close()
        self._retired_media_pools.append(self._thumb_pool)
        self._thumb_bridge.deleteLater()
        self._create_media_pool()
        self._clear_media_ram()
        self._schedule_media_retry()
        for cache in (
            self._thumbs,
            self._waves,
            self._strip_pix,
            self._strip_bounds,
            self._wave_failed,
        ):
            cache.clear()
        for pending in (self._thumb_pending, self._wave_pending, self._strip_pending):
            pending.clear()

    def _create_media_pool(self) -> None:
        self._media_generation += 1
        self._thumb_pool = ThumbnailPool(self)
        self._thumb_bridge = MediaResultBridge(self, self._media_generation)
        self._thumb_pool.signals.ready.connect(self._thumb_bridge.ready)
        self._thumb_pool.signals.failed.connect(self._thumb_bridge.failed)

    @Slot()
    def relink_source(self) -> None:
        if not self._project_can_start_io() or not self.project.assets:
            return
        labels = [f"{asset.name} — {asset.path}" for asset in self.project.assets]
        label, accepted = QInputDialog.getItem(
            self, "Заменить исходник", "Файл проекта:", labels, 0, False
        )
        if not accepted:
            return
        asset = self.project.assets[labels.index(label)]
        path, _ = QFileDialog.getOpenFileName(
            self, "Новый исходник", str(Path(asset.path).parent), "Медиа (*)"
        )
        if path:
            self._finish_edit_sessions()
            self._start_project_io(
                "relink", "", self._take_snapshot(), replacements={asset.iid: path}
            )

    @Slot()
    def relink_source_folder(self) -> None:
        if not self._project_can_start_io() or not self.project.assets:
            return
        folders = sorted(
            {str(Path(asset.path).absolute().parent) for asset in self.project.assets}
        )
        old, accepted = QInputDialog.getItem(
            self, "Перепривязать папку", "Прежняя папка:", folders, 0, False
        )
        if not accepted:
            return
        new = QFileDialog.getExistingDirectory(self, "Новая папка исходников", old)
        if not new:
            return
        replacements = {}
        for asset in self.project.assets:
            try:
                relative = Path(asset.path).absolute().relative_to(old)
            except ValueError:
                continue
            replacements[asset.iid] = str(Path(new) / relative)
        self._finish_edit_sessions()
        self._start_project_io(
            "relink", "", self._take_snapshot(), replacements=replacements
        )

    def _approve_project_close(self, result: int) -> None:
        self._project_close_approved = True
        self._request_close(result)
