"""Окно видеоредактора: сборка интерфейса, история правок, обновление и закрытие."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from PySide6.QtCore import QLoggingCategory, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.editing import ProjectCommands
from src.domain.video.models import (
    MediaAsset,
    Project,
)
from src.domain.video.undo_stack import VideoEditSnapshot, VideoUndoMixin
from src.logger import logger
from src.workers.video_editor.audio_analysis import AudioAnalysisWorker
from src.workers.video_editor.export_worker import ExportWorker
from src.workers.video_editor.ffmpeg_utils import (
    media_available,
)
from src.workers.video_editor.metadata import ProbeMetadata
from src.workers.video_editor.probe_worker import ProbeWorker
from src.workers.video_editor.render_command import RenderCommand

from .asset_actions import VideoAssetMixin
from .audio_controls import VideoAudioMixin
from .export_actions import VideoExportMixin
from .media_results import VideoMediaMixin
from .monitor_workspace import MonitorWorkspace
from .notice_bar import NoticeBar
from .overlay_actions import VideoOverlayMixin
from .preview_widget import _mk_tool_btn
from .project_actions import VideoProjectMixin
from .project_panel import ProjectPanel
from .recovery_actions import VideoRecoveryMixin
from .series_controller import SeriesController
from .text_overlay_widget import TextOverlayWidget
from .timeline import TimelineWidget
from .timeline_actions import VideoTimelineMixin

# A caller may drop its reference after close/reject. Keep the QObject parent
# of live QThreads owned until the GUI timer observes their actual completion.
_CLOSING_DIALOGS: set[VideoEditorDialog] = set()


class VideoEditorDialog(
    QDialog,
    VideoUndoMixin,
    VideoProjectMixin,
    VideoRecoveryMixin,
    VideoMediaMixin,
    VideoAssetMixin,
    VideoTimelineMixin,
    VideoAudioMixin,
    VideoOverlayMixin,
    VideoExportMixin,
):
    """Модальный видеоредактор. Открывается через exec()."""

    _log_rules_applied: ClassVar[bool] = False

    _probe_requested = Signal(str, str)
    close_cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Видеоредактор")
        self.setModal(True)
        # полноценное окно: свернуть/развернуть, как у обычного окна,
        # а не только close у QDialog по умолчанию
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # обычное окно, НЕ на весь экран
        self.resize(1200, 800)
        # FFmpeg-бэкенд QtMultimedia пишет av_log в консоль
        # ("Input #0...", "MFT name...") через подкатегории
        # qt.multimedia.ffmpeg.* - глушим их, остальное не трогаем
        if not getattr(VideoEditorDialog, "_log_rules_applied", False):
            QLoggingCategory.setFilterRules(
                "qt.multimedia.ffmpeg=false\nqt.multimedia.ffmpeg.*=false"
            )
            VideoEditorDialog._log_rules_applied = True
        self.setAcceptDrops(True)
        self.project = Project()
        self._init_undo()
        self._assets: dict[str, MediaAsset] = {}
        self._sel_clip: str | None = None
        self._sel_text: str | None = None
        self._sel_track: str | None = None
        self._playhead = 0.0
        self._mark_in: float | None = None
        self._mark_out: float | None = None
        self._vol_before: dict[str, Any] | None = (
            None  # снапшот для undo одного драга слайдера
        )
        self._master_before: dict[str, Any] | None = None
        self._text_before: dict[str, Any] | None = None
        self._init_project_state()
        self._init_recovery_state()
        self._init_media_results()
        self._probe_requests: dict[str, tuple[str, str]] = {}
        self._probe_latest: dict[str, str] = {}
        self._probe_cache: dict[tuple[str, str], ProbeMetadata] = {}
        self._probe_errors: dict[tuple[str, str], str] = {}
        self._last_preview_audio: tuple[float, bool, float] | None = (
            None  # кеш (vol, mute, master)
        )
        self._export_worker: ExportWorker | None = None
        self._export_thread: QThread | None = None
        self._audio_analysis: AudioAnalysisWorker | None = None
        self._series: SeriesController | None = None
        self._last_export_report = ""
        self._closing = False
        self._close_complete = False
        self._close_result = int(QDialog.DialogCode.Rejected)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(25)
        self._close_timer.timeout.connect(self._poll_close)
        self._op_cancel = threading.Event()  # отмена экспорта/ремонта
        self._repair_in_progress = False
        self._repair_thread: threading.Thread | None = None
        self._repair_timer: QTimer | None = None
        self._repair_failed: set[str] = set()
        self._repair_queue: list[tuple[str, list[str]]] = []
        # миниатюры списка файлов (первый кадр) и волны аудиоклипов
        self._thumbs: dict[str, QIcon] = {}
        self._thumb_pending: set[str] = set()
        self._waves: dict[str, QPixmap] = {}
        self._wave_pending: set[str] = set()
        # ленты превью видеоклипов (filmstrip): pix по clip iid + границы
        # src на момент генерации; trim мимо кэша - перезапрос
        self._strip_pix: dict[str, QPixmap] = {}
        self._strip_bounds: dict[str, tuple[float, float]] = {}
        self._strip_pending: set[str] = set()
        # волны с ошибкой: повторный запрос не раньше _WAVE_RETRY_S,
        # иначе обновления UI повторно запускают анализ; явный переанализ
        # через _forget_media_cache сбрасывает бан сразу
        self._wave_failed: dict[str, float] = {}
        self._create_media_pool()
        # debounce печати текста: превью не дёргать на каждый символ
        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(30)
        self._text_timer.timeout.connect(self._do_text_changed)
        self._text_commit_timer = QTimer(self)
        self._text_commit_timer.setSingleShot(True)
        self._text_commit_timer.setInterval(500)
        self._text_commit_timer.timeout.connect(self._commit_text_edit)
        self._build_ui()
        for button in self.findChildren(QToolButton):
            if not button.accessibleName():
                button.setAccessibleName(button.toolTip() or button.text())
        self._restore_editor_ui()
        self._connect_media_visibility()
        self._build_workers()
        self._build_shortcuts()
        for signal in (
            self.undo_stack.canUndoChanged,
            self.undo_stack.canRedoChanged,
            self.undo_stack.undoTextChanged,
            self.undo_stack.redoTextChanged,
        ):
            signal.connect(self._update_history_controls)
        self._update_history_controls()
        self.refresh_all()
        self._sync_audio_controls()
        self._request_recovery("catalog")
        if not media_available():
            from src.ui.dialogs.tools.ffmpeg import (
                show_media_diagnostics,
            )

            if not show_media_diagnostics(
                self, "Не найден полный встроенный комплект медиабиблиотек."
            ):
                QMessageBox.warning(
                    self,
                    "FFmpeg",
                    "Медиадвижок недоступен. Проверьте поставку приложения в диагностике FFmpeg / PyAV.",
                )

    # -- UI --
    @property
    def edits(self) -> ProjectCommands:
        return ProjectCommands(self.project)

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        header = QGridLayout()
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        header.addWidget(self._build_project_menu(), 0, 0)
        lay.addLayout(header)
        self.notice = NoticeBar(self)
        lay.addWidget(self.notice)

        mid = QSplitter(Qt.Orientation.Horizontal)
        self._mid_splitter = mid
        self.panel = ProjectPanel()
        self.panel.import_requested.connect(self.import_files)
        self.panel.delete_file_requested.connect(self.delete_selected_asset)
        self.panel.clear_list_requested.connect(self.clear_assets)
        self.panel.remove_requested.connect(self.remove_asset)
        self.panel.repair_requested.connect(self.repair_asset)
        self.panel.reprobe_requested.connect(self.reprobe_asset)
        self.panel.add_to_timeline_requested.connect(self.append_asset_to_timeline)
        self.panel.add_all_to_timeline_requested.connect(self.append_all_to_timeline)
        self.panel.add_batch_requested.connect(self.append_assets_to_timeline)
        self.panel.remove_batch_requested.connect(self.remove_assets)
        self.panel.reprobe_batch_requested.connect(self.reprobe_assets)
        self.panel.title_requested.connect(self.make_asset_title)
        self.panel.source_preview_requested.connect(self.preview_source)
        self.panel.list.currentItemChanged.connect(
            lambda current, previous: self._preview_current_source()
        )
        self.panel.show_in_folder_requested.connect(self.show_asset_in_folder)
        self.panel.file_info_requested.connect(self.show_asset_info)
        self.panel.rename_requested.connect(self.rename_asset)
        mid.addWidget(self.panel)
        self.monitors = MonitorWorkspace(self)
        self.source_monitor = self.monitors.source
        self.preview = self.monitors.program
        self.source_monitor.append_requested.connect(self.append_source_selection)
        self.monitors.mode_changed.connect(
            lambda mode: self._monitor_actions[mode].setChecked(True)
        )
        header.addWidget(self.monitors.mode_buttons, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.preview.view.title_dragged.connect(
            lambda dx, dy: self._advanced.title_dragged(dx, dy)
        )
        self.preview.view.transform_committed.connect(
            lambda x, y, scale: self.inspector_controller.move_and_scale(x, y, scale)
        )
        self.preview.seek_requested.connect(self._on_preview_seek)
        self.preview.timeline_pos.connect(self._on_playback_pos)
        self.preview.mark_in_requested.connect(lambda: self._set_mark("in"))
        self.preview.mark_out_requested.connect(lambda: self._set_mark("out"))
        self.preview.split_requested.connect(self.edit_actions.actions["split"].trigger)
        self.preview.delete_requested.connect(
            self.edit_actions.actions["delete"].trigger
        )
        self.preview.cut_range_requested.connect(
            self.edit_actions.actions["cut_range"].trigger
        )
        self.preview.keep_range_requested.connect(
            self.edit_actions.actions["keep_range"].trigger
        )
        mid.addWidget(self.monitors)
        self.text_panel = TextOverlayWidget()
        self.text_panel.changed.connect(self._on_text_changed)
        self.text_panel.overlay_selected.connect(self._on_text_selected)
        self.text_panel.gesture_started.connect(self._start_overlay_gesture)
        self.text_panel.gesture_finished.connect(self._finish_overlay_gesture)
        self.text_panel.input_finished.connect(self._commit_text_edit)
        self._overlay_gesture = False
        # Справа — инспектор выбранного объекта и библиотека наложений.
        from .inspector import InspectorController, InspectorWidget

        self.inspector = InspectorWidget(self.text_panel, self)
        self.inspector_controller = InspectorController(self)
        text_col = self.inspector
        self._text_column = text_col
        self.btn_export = _mk_tool_btn("ve_export", "Экспорт", self)
        self.btn_export.clicked.connect(self.do_export)
        header.addWidget(self.btn_export, 0, 2, Qt.AlignmentFlag.AlignRight)
        mid.addWidget(text_col)
        mid.setStretchFactor(0, 0)
        mid.setStretchFactor(1, 1)
        mid.setStretchFactor(2, 0)
        mid.setSizes([200, 670, 300])

        # низ: слева панель звука (Мастер, затем Клип), справа таймлайн.
        # Столбики вертикальных слайдеров тянутся на всю высоту области,
        # одинаковой высоты; кнопки mute - внизу каждого столбика.
        tl_pane = QWidget()
        tl_lay = QHBoxLayout(tl_pane)
        tl_lay.setContentsMargins(0, 0, 0, 0)
        audio = QWidget()
        audio.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Expanding)
        self._audio_panel = audio
        # вертикальная структура: столбики ползунков сверху (тянутся),
        # mute-ряд прижат в самый низ
        audio_lay = QVBoxLayout(audio)
        audio_lay.setContentsMargins(8, 4, 8, 4)
        audio_lay.setSpacing(4)

        def _vol_col(
            label: str, slider: QSlider, value: QLabel, mute: QToolButton
        ) -> QVBoxLayout:
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(QLabel(label), 0, Qt.AlignmentFlag.AlignHCenter)
            slider.setOrientation(Qt.Orientation.Vertical)
            # фикс ширина; высота растягивается со всей областью
            slider.setFixedWidth(28)
            col.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(value, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(mute, 0, Qt.AlignmentFlag.AlignHCenter)
            return col

        self.master_slider = QSlider()
        self.master_slider.setRange(0, 200)
        self.master_slider.setValue(100)
        self.master_slider.setToolTip("Мастер-громкость, %")
        self.master_slider.valueChanged.connect(self._on_master_slider)
        self.master_slider.sliderPressed.connect(self._on_master_press)
        self.master_slider.sliderReleased.connect(self._on_master_release)
        self.master_value = QLabel("100%")
        self.btn_master_mute = _mk_tool_btn("ve_volume", "Mute мастера", self)
        self.btn_master_mute.setCheckable(True)
        self.btn_master_mute.toggled.connect(self._on_master_mute_toggled)
        self.vol_slider = QSlider()
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setValue(100)
        self.vol_slider.setToolTip("Громкость клипа, % (выделите клип на таймлайне)")
        self.vol_slider.setEnabled(False)
        self.vol_slider.valueChanged.connect(self._on_vol_slider)
        self.vol_slider.sliderPressed.connect(self._on_vol_press)
        self.vol_slider.sliderReleased.connect(self._on_vol_release)
        self.vol_value = QLabel("-")
        self.btn_mute = _mk_tool_btn("ve_volume", "Mute клипа", self)
        self.btn_mute.setCheckable(True)
        self.btn_mute.setEnabled(False)
        self.btn_mute.toggled.connect(self._on_mute_toggled)
        self._sync_mute_icon(False)
        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(8)
        cols.addLayout(
            _vol_col(
                "Мастер", self.master_slider, self.master_value, self.btn_master_mute
            )
        )
        cols.addLayout(_vol_col("Клип", self.vol_slider, self.vol_value, self.btn_mute))
        audio_lay.addLayout(cols, 1)
        self.limiter = QCheckBox("Лимитер", self)
        self.limiter.setToolTip(
            "Явно ограничивать пики до −0.45 dBFS (0.95). Одинаково для предпросмотра и экспорта; автоматическое повышение уровня выключено."
        )
        self.limiter.toggled.connect(self._set_audio_limiter)
        audio_lay.addWidget(self.limiter)
        tl_lay.addWidget(audio)

        self.timeline = TimelineWidget()
        self.timeline.seeked.connect(self._on_timeline_seek)
        self.timeline.clip_edit_requested.connect(self._on_timeline_edited)
        self.timeline.clip_selected.connect(self._on_clip_selected)
        self.timeline.text_selected.connect(self._on_text_selected)
        self.timeline.text_edit_requested.connect(self._on_text_interval_edited)
        self.timeline.clip_action_requested.connect(self._on_clip_action)
        self.timeline.drop_asset.connect(self.add_asset_to_timeline)
        self.timeline.track_selected.connect(self._on_track_selected)
        self.timeline.split_clip_requested.connect(self._split_clip)
        self.timeline.context_menu_factory = (
            lambda kind: self.edit_actions.context_menu(kind)
        )
        self.edit_actions.build_toolbar()
        tl_lay.addWidget(self.timeline, 1)

        # вертикальный сплиттер: превью главнее, таймлайн можно ужать;
        # коллапс запрещен у обеих областей
        main = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter = main
        main.addWidget(mid)
        main.addWidget(tl_pane)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)
        main.setChildrenCollapsible(False)
        main.setSizes([410, 330])
        lay.addWidget(main, 1)

        # прогресс + кнопка отмены (прижата вправо): экспорт или ремонт
        prog_row = QWidget()
        prog_lay = QHBoxLayout(prog_row)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.hide()
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.hide()
        self.btn_cancel.clicked.connect(self._cancel_operation)
        prog_lay.addWidget(self.progress, 1)
        prog_lay.addWidget(self.btn_cancel)
        lay.addWidget(prog_row)
        prog_row.hide()

    def _restore_editor_ui(self) -> None:
        ancestor = self.parentWidget()
        self._editor_view_state = None
        while ancestor is not None:
            if hasattr(ancestor, "view_state"):
                self._editor_view_state = ancestor.view_state
                break
            ancestor = ancestor.parentWidget()
        if self._editor_view_state is None:
            return
        state = self._editor_view_state
        state.register("video_editor", persist=True)
        geometry = state.get("video_editor", "geometry")
        if geometry:
            self.restoreGeometry(geometry)
        for key, splitter in (
            ("horizontal", self._mid_splitter),
            ("vertical", self._main_splitter),
        ):
            sizes = state.get("video_editor", key)
            if isinstance(sizes, list) and all(
                type(value) is int and value >= 0 for value in sizes
            ):
                splitter.setSizes(sizes)
        self._show_media_action.setChecked(
            state.get("video_editor", "show_media", True)
        )
        self._show_text_action.setChecked(
            state.get(
                "video_editor",
                "show_inspector",
                state.get("video_editor", "show_text", True),
            )
        )
        self.monitors.restore_sizes(state.get("video_editor", "monitor_sizes"))
        self.monitors.set_mode(state.get("video_editor", "monitor_mode", "dual"))

    def _save_editor_ui(self) -> None:
        state = self._editor_view_state
        if state is None:
            return
        for key, value in (
            ("geometry", self.saveGeometry()),
            ("horizontal", self._mid_splitter.sizes()),
            ("vertical", self._main_splitter.sizes()),
            ("show_media", self._show_media_action.isChecked()),
            ("show_inspector", self._show_text_action.isChecked()),
            ("monitor_mode", self.monitors.mode),
            ("monitor_sizes", self.monitors.layout_sizes()),
        ):
            state.set("video_editor", key, value, track_change=False)

    def _build_workers(self) -> None:
        self._probe_thread = QThread(self)
        self._probe = ProbeWorker()
        self._probe.moveToThread(self._probe_thread)
        self._probe_thread.finished.connect(self._probe.deleteLater)
        self._probe_requested.connect(
            self._probe.probe, Qt.ConnectionType.QueuedConnection
        )
        self._probe.probed.connect(self._on_probed)
        self._probe.failed.connect(self._on_probe_failed)
        self._probe_thread.start()

    def _build_shortcuts(self) -> None:
        self._editing_shortcuts: list[QShortcut] = []

        def sc(seq: str, fn: Callable[[], None]) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            s.activated.connect(fn)
            self._editing_shortcuts.append(s)

        sc("I", lambda: self._mark_active_monitor("in"))
        sc("Ш", lambda: self._mark_active_monitor("in"))
        sc("O", lambda: self._mark_active_monitor("out"))
        sc("Щ", lambda: self._mark_active_monitor("out"))
        sc("Space", lambda: self._active_monitor().toggle())
        sc("Left", lambda: self._active_monitor().step_frame(-1))
        sc("Right", lambda: self._active_monitor().step_frame(1))
        sc("Home", lambda: self._seek_active_monitor(end=False))
        sc("End", lambda: self._seek_active_monitor(end=True))
        sc("Up", lambda: self._seek_boundary(-1))
        sc("Down", lambda: self._seek_boundary(1))
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.focusChanged.connect(self._on_editor_focus_changed)
        self._on_editor_focus_changed(None, QApplication.focusWidget())

    @Slot(QWidget, QWidget)
    def _on_editor_focus_changed(
        self, old: QWidget | None, current: QWidget | None
    ) -> None:
        if (
            old is not None
            and self.text_panel.isAncestorOf(old)
            and (current is None or not self.text_panel.isAncestorOf(current))
        ):
            self._commit_text_edit()
        widget = current
        typing = False
        while widget is not None and widget is not self:
            if isinstance(
                widget,
                (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
            ):
                typing = True
                break
            widget = widget.parentWidget()
        self.edit_actions.typing(
            typing or self._closing or bool(current and current.window() is not self)
        )
        for shortcut in self._editing_shortcuts:
            shortcut.setEnabled(
                not typing
                and not self._closing
                and (current is None or current.window() is self)
            )

    @Slot()
    def _update_history_controls(self) -> None:
        pending = self._text_before is not None
        self._undo_action.setEnabled(self.undo_stack.canUndo() or pending)
        self._redo_action.setEnabled(self.undo_stack.canRedo() and not pending)
        undo_text = "Текст" if pending else self.undo_stack.undoText()
        redo_text = self.undo_stack.redoText() if not pending else ""
        for action, label, text, shortcut in (
            (self._undo_action, "Отменить", undo_text, "Ctrl+Z"),
            (self._redo_action, "Повторить", redo_text, "Ctrl+Y"),
        ):
            title = label + (f": {text}" if text else "")
            action.setText(f"{title}\t{shortcut}")
            action.setToolTip(title)
        self._update_editor_actions()

    def _update_editor_actions(self) -> None:
        duration = self.project.duration()
        available = duration > 0 and not self._closing
        for button in (
            self.preview.btn_play,
            self.preview.btn_pause,
            self.preview.btn_stop,
            self.preview.btn_prev,
            self.preview.btn_next,
            self.preview.btn_start,
            self.preview.btn_mark_in,
            self.preview.btn_mark_out,
        ):
            button.setEnabled(available)
        busy = (
            self._thread_is_running(self._export_thread)
            or self._repair_in_progress
            or self._audio_analysis is not None
            or (self._series is not None and self._series.active)
        )
        self.btn_export.setEnabled(available and not busy)
        self.edit_actions.update()
        self._sync_transform_box()

    def _sync_transform_box(self) -> None:
        """Рамка видна у выбранного незаблокированного клипа под курсором."""
        clip = self.inspector_controller.picture_clip()
        visible = False
        if clip is not None and clip.timeline_start <= self._playhead < clip.timeline_end:
            tracks = [
                track
                for track in self.project.tracks
                if any(
                    item.iid == clip.iid
                    or (clip.link_iid and item.link_iid == clip.link_iid)
                    for item in track.clips
                )
            ]
            visible = not any(
                track.locked or (track.hidden and track.kind == "video")
                for track in tracks
            )
        self.preview.set_transform_box(
            (clip.position_x, clip.position_y, clip.scale) if visible else None
        )

    # -- undo mixin --
    def _take_snapshot(self) -> dict[str, Any]:
        self._commit_text_edit()
        return VideoEditSnapshot(
            VideoUndoMixin._take_snapshot(self),
            [self._sel_clip, self._sel_text, self._sel_track],
        )

    def _restore_snapshot(self, snapshot: dict) -> None:
        self._sel_clip, self._sel_text, self._sel_track = snapshot.get(
            "_selection", [None, None, None]
        )
        VideoUndoMixin._restore_snapshot(self, snapshot)

    def undo_edit(self) -> None:
        self._finish_edit_sessions()
        self.undo_stack.undo()

    def redo_edit(self) -> None:
        self._finish_edit_sessions()
        self.undo_stack.redo()

    def _finish_edit_sessions(self) -> None:
        if hasattr(self, "inspector"):
            self.inspector.finish()
        self._overlay_gesture = False
        self._commit_text_edit()
        self._on_vol_release()
        self._on_master_release()

    def _on_edit_committed(self, refresh: bool) -> None:
        if refresh:
            self._on_project_restored()
        self._update_history_controls()
        self._update_project_title()

    def _on_project_restored(self) -> None:
        self._assets = {a.iid: a for a in self.project.assets}
        for a in self.project.assets:
            key = (a.iid, a.path)
            if key in self._probe_cache:
                self._apply_probe_metadata(a, self._probe_cache[key])
            elif key in self._probe_errors:
                a.probe_error = self._probe_errors[key]
            elif a.duration <= 0:
                self._queue_probe(a.iid, a.path)
        # кэши миниатюр/волн - только для живых ассетов
        live = set(self._assets)
        for d in (self._thumbs, self._waves):
            for k in [k for k in d if k not in live]:
                d.pop(k, None)
        self._thumb_pending.intersection_update(live)
        self._wave_pending.intersection_update(live)
        for k in [k for k in self._wave_failed if k not in live]:
            self._wave_failed.pop(k, None)
        live_clips = {c.iid for tr in self.project.tracks for c in tr.clips}
        for k in [k for k in self._strip_pix if k not in live_clips]:
            self._strip_pix.pop(k, None)
            self._strip_bounds.pop(k, None)
        self._strip_pending.intersection_update(live_clips)
        for mode, media_key in list(self._media_ram):
            if media_key not in (live_clips if mode == "strip" else live):
                self._forget_media_ram(media_key)
        self.refresh_all()
        self._update_project_title()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and Path(p).is_file():
                self._import_one(p)

    # -- refresh --
    def refresh_all(self, no_undo: bool = False) -> None:
        if self._closing:
            return
        if self._text_before is not None:
            self._do_text_changed()
        self.panel.set_assets(
            self.project.assets,
            self._thumbs,
            {clip.asset_iid for track in self.project.tracks for clip in track.clips},
        )
        if self.source_monitor.asset is not None:
            asset = self.project.asset_by_iid(self.source_monitor.asset.iid)
            if asset is None:
                self.source_monitor.clear()
            elif asset.duration > 0:
                self.source_monitor.load_asset(
                    asset, self._probe_cache.get((asset.iid, asset.path))
                )
        self._sync_source_watches()
        # протухшие ленты (клип удален) - выкинуть, иначе кэш растет
        live_clips = {c.iid for tr in self.project.tracks for c in tr.clips}
        for k in [k for k in self._strip_pix if k not in live_clips]:
            self._strip_pix.pop(k, None)
            self._strip_bounds.pop(k, None)
        self._strip_pending.intersection_update(live_clips)
        self._refreshing_selection = True
        self.timeline.set_data(
            self.project.tracks,
            self._assets,
            self._waves,
            self._strip_pix,
            self.project.markers,
            list(self.project.text_overlays),
        )
        self.timeline.select_clip(self._sel_clip or self._sel_text or "")
        self.timeline._selected_track = self._sel_track if not self._sel_clip else None
        self._refreshing_selection = False
        self._update_media_visibility()
        # Повтор для миниатюр/волн/лент: разовый сбой медиадвижка,
        # файл был заблокирован) больше не оставляет список/таймлайн
        # пустыми - дедуп внутри _request_* делает проход дешевым
        a: MediaAsset | None
        for a in self.project.assets:
            if a.duration <= 0:
                continue
            if a.kind == "video" and a.has_video:
                self._request_thumb(a.iid)
        for tr in self.project.tracks:
            for c in tr.clips:
                a = self._assets.get(c.asset_iid)
                if a is None or a.duration <= 0:
                    continue
                if tr.kind == "audio":
                    # волна для чистого аудио и звука видеофайлов (пары V/A)
                    self._request_wave(a.iid)
                elif tr.kind == "video":
                    self._request_strip(c, a)
        self.timeline.set_playhead(self._playhead)
        self.master_slider.blockSignals(True)
        self.master_slider.setValue(round(self.project.master_volume * 100))
        self.master_slider.blockSignals(False)
        self.master_value.setText(f"{round(self.project.master_volume * 100)}%")
        self.limiter.blockSignals(True)
        self.limiter.setChecked(self.project.audio_limiter)
        self.limiter.blockSignals(False)
        self._sync_audio_controls()
        self.text_panel.set_overlays(list(self.project.text_overlays))
        self._sync_overlay_locks()
        self.inspector_controller.refresh()
        self._apply_audio_at_playhead()
        self.refresh_preview()

        self._update_editor_actions()

    def refresh_preview(self) -> None:
        from src.domain.video.render_plan import RenderPlan

        try:
            plan = RenderPlan.from_project(self.project)
        except ValueError as error:
            self.preview.set_render_plan(None, None)
            self.preview.err_label.setText(str(error))
            self.preview.err_label.show()
            return
        reference = plan.canvas_source
        if reference is not None:
            metadata = {
                asset.path: self._probe_cache.get(
                    (asset.iid, asset.path),
                    {
                        "width": asset.width or 640,
                        "height": asset.height or 360,
                        "fps": asset.fps or 25,
                    },
                )
                for asset in self.project.assets
            }
            try:
                width, height, _ = RenderCommand.canvas(plan, metadata, {})
                self.preview.set_video_size(width, height)
            except (ValueError, KeyError, IndexError):
                self.preview.set_video_size(0, 0)
        self.preview.set_render_plan(plan, None)

    # -- закрытие --
    @staticmethod
    def _thread_is_running(thread: QThread | None) -> bool:
        if thread is None:
            return False
        try:
            return not thread.wait(0)
        except RuntimeError:
            return False  # Qt already deleted the finished thread.

    def has_pending_workers(self) -> bool:
        """Check owned thread lifetimes on the GUI thread before app teardown."""
        self._retire_media_pools()
        threads = [self._probe_thread, self._export_thread, *self.findChildren(QThread)]
        if self._export_worker is not None and self._export_thread is None:
            try:
                threads.append(self._export_worker.thread())
            except RuntimeError:
                pass
        return (
            (isinstance(self._probe, ProbeWorker) and self._probe.pending_count > 0)
            or self._thumb_pool.pending_count > 0
            or any(pool.pending_count > 0 for pool in self._retired_media_pools)
            or self._project_io_thread is not None
            or (
                self.preview.render_controller is not None
                and self.preview.render_controller.worker is not None
            )
            or self._recovery_thread is not None
            or self._recovery_pending is not None
            or any(self._thread_is_running(thread) for thread in threads)
            or (self._repair_thread is not None and self._repair_thread.is_alive())
        )

    def _request_close(self, result: int) -> None:
        if self._close_complete:
            return
        if not self._closing and not self._project_close_approved:
            if (
                self._guard_project_action(lambda: self._approve_project_close(result))
                is False
            ):
                self.close_cancelled.emit()
            return
        if not self._closing:
            self._finish_edit_sessions()
            self._save_editor_ui()
            self._closing = True
            self._close_recovery()
            self._close_result = result
            _CLOSING_DIALOGS.add(self)
            self.setEnabled(False)
            self._text_timer.stop()
            self._text_commit_timer.stop()
            self._media_retry_timer.stop()
            self._source_timer.stop()
            self._op_cancel.set()
            self._repair_queue.clear()
            actions = [
                self.source_monitor.shutdown,
                self.preview.shutdown,
                self._thumb_pool.close,
                self._probe.close,
                self._probe_thread.quit,
            ]
            if self._export_worker is not None:
                actions.append(self._export_worker.cancel)
                try:
                    self._export_thread = self._export_worker.thread()
                    if self._export_thread is not None:
                        actions.append(self._export_thread.quit)
                except RuntimeError:
                    pass
            if self._audio_analysis is not None:
                self._audio_analysis.cancelled.set()
            if self._series is not None:
                self._series.cancel(all_jobs=True)
            for action in actions:
                try:
                    action()
                except Exception as error:  # noqa: BLE001 - keep other cleanup running
                    logger.error(f"video editor close: {error}")
            self._close_timer.start()
        self._poll_close()

    @Slot()
    def _poll_close(self) -> None:
        if not self._closing or self._close_complete or self.has_pending_workers():
            return
        self._close_timer.stop()
        if self._repair_timer is not None:
            self._repair_timer.stop()
        self._export_worker = None
        self._export_thread = None
        self._repair_thread = None
        self._close_complete = True
        _CLOSING_DIALOGS.discard(self)
        super().done(self._close_result)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self._request_close(int(QDialog.DialogCode.Rejected))
        if self._close_complete:
            event.accept()

    def reject(self) -> None:
        self._request_close(int(QDialog.DialogCode.Rejected))

    def done(self, result: int) -> None:
        self._request_close(result)
