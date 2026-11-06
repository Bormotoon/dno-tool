"""Окно камер ЕЦХД: отображение состояния независимого менеджера записи."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QHideEvent, QIcon, QPainter, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.architecture import ViewState
from src.services.remote.camera_recording import Recording
from src.ui.dialogs.tools.camera_preview_controller import CameraPreviewController
from src.ui.dialogs.tools.camera_selector import CameraSelectionDialog
from src.ui.dialogs.tools.camera_wall import (
    CAMERA_FOCUS_LAYOUT,
    CAMERA_GRID_MODES,
    CAMERA_VIEW_MODES,
    CameraWall,
)
from src.ui.icons import _icon_from_key, set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.utils.system.subprocess_env import (
    build_clean_env,
    safe_cwd_for_user,
    wrap_as_user,
)
from src.workers.camera_workers import (
    build_player_cmd,
    launch_player,
    launch_system_player,
    resolve_player_executable,
)

STATE_TEXT = {
    "idle": "Ожидание",
    "starting": "Подключение…",
    "recording": "Запись",
    "stopping": "Останавливается…",
    "reconnecting": "Повтор подключения",
    "error": "Ошибка",
}
COL_ADDRESS, COL_CAMERA, COL_ONLINE, COL_STATUS, COL_TIME, COL_SIZE = range(6)


def _recording_elapsed(record: Recording) -> str:
    """Одинаковая длительность записи в списке и на видеостене."""
    if not record.started:
        return ""
    seconds = max(
        0, int(time.time() - record.started if record.worker else record.elapsed)
    )
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def _state_icon(color: QColor | None, size: int = 12) -> QIcon:
    """Цветной индикатор состояния записи в строке камеры."""
    if color is None:
        return QIcon()
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def _human_size(size):
    value = float(size)
    for suffix in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or suffix == "ТиБ":
            return f"{value:.1f} {suffix}"
        value /= 1024


def _open_in_file_manager(path: Path) -> bool:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        else:
            kwargs = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": safe_cwd_for_user(),
            }
            env = build_clean_env()
            if env is not None:
                kwargs["env"] = env
            subprocess.Popen(
                wrap_as_user(
                    ["open" if sys.platform == "darwin" else "xdg-open", str(path)]
                ),
                **kwargs,
            )
        return True
    except OSError:
        return False


class CamerasDialog(QWidget):
    """Окно можно скрывать: записью продолжает владеть CameraService."""

    def __init__(self, camera_service, event_bus=None, parent=None, view_state: ViewState | None = None):
        super().__init__(parent)
        self.setWindowTitle("Камеры ЕЦХД")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(700, 520)
        self._svc = camera_service
        self._manager = camera_service.recording_manager()
        self._item_map = {}
        self._structure = None
        self._view_mode = 0
        self._view_state = view_state if view_state is not None else getattr(parent, "view_state", None)
        if self._view_state is None:
            self._view_state = ViewState()
        self._view_state.register("cameras_view", persist=True)
        self._restoring_wall = True
        saved_mode = self._view_state.get("cameras_view", "mode", 0)
        if type(saved_mode) is not int or saved_mode not in CAMERA_VIEW_MODES:
            saved_mode = 0
        self._previous_grid = self._view_state.get("cameras_view", "previous_grid", 4)
        if type(self._previous_grid) is not int or self._previous_grid not in CAMERA_GRID_MODES:
            self._previous_grid = 4
        self._wall_signature = None
        self._preview = CameraPreviewController(self)
        layout = QVBoxLayout(self)
        self.setMinimumSize(600, 400)
        if self._svc.load_error:
            error_label = QLabel(self._svc.load_error)
            error_label.setWordWrap(True)
            layout.addWidget(error_label)
        toolbar_widget = QWidget(self)
        toolbar_widget.setFixedHeight(36)
        toolbar_widget.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(2)
        settings = QToolButton(self)
        set_themed_icon(settings, "menu_settings")
        settings.setToolTip("Настройки")
        settings.setAccessibleName("Настройки")
        settings.clicked.connect(self._open_settings)
        toolbar.addWidget(settings)
        self._mode_buttons = QButtonGroup(self)
        for count in CAMERA_VIEW_MODES:
            button = QToolButton(self)
            suffix = "6_focus" if count == CAMERA_FOCUS_LAYOUT else count or "list"
            set_themed_icon(button, f"camera_view_{suffix}", toggle=True)
            button.setCheckable(True)
            if count == CAMERA_FOCUS_LAYOUT:
                tip = "Наблюдение: 6 камер — большая справа внизу, малые сверху и слева"
            elif count == 6:
                tip = "Наблюдение: 6 камер — сетка 3×2"
            else:
                tip = (
                    "Список камер" if count == 0 else f"Наблюдение: {count} "
                    + ("камера" if count == 1 else "камеры" if count == 4 else "камер")
                )
            button.setToolTip(tip)
            button.setAccessibleName(button.toolTip())
            self._mode_buttons.addButton(button, count)
            toolbar.addWidget(button)
        self._record_wall_button = QToolButton(self)
        set_themed_icon(self._record_wall_button, "menu_camera_record")
        self._record_wall_button.setText("Записать доску")
        self._record_wall_button.setToolTip(
            "Начать запись всех камер на текущей доске. Уже запущенные записи продолжаются."
        )
        self._record_wall_button.setEnabled(False)
        self._record_wall_button.clicked.connect(self._start_wall_recording)
        toolbar.addWidget(self._record_wall_button)
        stop = QToolButton(self)
        set_themed_icon(stop, "menu_camera_stop")
        stop.setToolTip("Остановить все")
        stop.setAccessibleName("Остановить все")
        stop.clicked.connect(self._manager.stop_all)
        toolbar.addWidget(stop)
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._summary_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(self._summary_label, 1)
        self._previous_page_button = QToolButton(self)
        self._next_page_button = QToolButton(self)
        for button, key, text in (
            (self._previous_page_button, "menu_back", "Предыдущая страница камер"),
            (self._next_page_button, "menu_forward", "Следующая страница камер"),
        ):
            set_themed_icon(button, key)
            button.setToolTip(text)
            button.setAccessibleName(text)
            button.setVisible(False)
            toolbar.addWidget(button)
        self._previous_page_button.clicked.connect(lambda: self._wall._move(-1))
        self._next_page_button.clicked.connect(lambda: self._wall._move(1))
        self._mode_buttons.button(0).setChecked(True)
        self._mode_buttons.idClicked.connect(self._set_view_mode)
        for button in (
            settings,
            *self._mode_buttons.buttons(),
            self._record_wall_button,
            stop,
            self._previous_page_button,
            self._next_page_button,
        ):
            button.setAutoRaise(True)
            button.setIconSize(QSize(32, 32))
            button.setFixedSize(32, 32)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._record_wall_button.setAccessibleName("Записать доску")
        layout.addWidget(toolbar_widget)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Адрес", "Камера", "Онлайн", "Статус", "Время", "Размер"]
        )
        self._tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._tree.header().setSectionsMovable(True)
        for col, width in enumerate((175, 100, 60, 125, 80, 90)):
            self._tree.setColumnWidth(col, width)
        self._tree.header().setSectionResizeMode(
            COL_SIZE, QHeaderView.ResizeMode.Stretch
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_popup)
        self._pages = QStackedWidget(self)
        self._pages.addWidget(self._tree)
        self._wall = CameraWall(self)
        self._wall.restore_presets(
            self._view_state.get("cameras_view", "presets", {}),
            self._view_state.get("cameras_view", "pages", {}),
        )
        self._pages.addWidget(self._wall)
        self._wall.visible_changed.connect(self._sync_preview)
        self._wall.visible_changed.connect(self._save_wall_state)
        self._wall.presets_changed.connect(self._save_wall_state)
        self._tree.currentItemChanged.connect(self._tree_camera_selected)
        self._wall.focus_requested.connect(self._focus_camera)
        self._wall.choose_requested.connect(self._select_wall_camera)
        self._wall.menu_requested.connect(self._show_wall_popup)
        self._preview.updated.connect(self._wall.update_frame)
        layout.addWidget(self._pages, 1)
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        layout.addWidget(self._status_label)
        self._preview.notice.connect(self._show_status_message)
        self._manager.changed.connect(self._refresh)
        self._manager.message.connect(self._show_status_message)
        self._refresh()
        if saved_mode:
            self._set_view_mode(saved_mode)
        self._restoring_wall = False

    def _save_wall_state(self) -> None:
        if self._restoring_wall:
            return
        for key, value in (
            ("presets", self._wall.presets()),
            ("pages", self._wall.pages()),
            ("mode", self._view_mode),
            ("previous_grid", self._previous_grid),
        ):
            self._view_state.set("cameras_view", key, value, track_change=False)

    def _tree_camera_selected(self) -> None:
        if self._view_mode:
            return
        item = self._tree.currentItem()
        iid = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        record = self._manager.records.get(iid) if isinstance(iid, str) else None
        if record is not None and not record.removed:
            self._wall.set_single_camera(iid)

    def _show_status_message(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setVisible(bool(message))

    def _set_view_mode(self, count: int, anchor: str = "") -> None:
        if count not in CAMERA_VIEW_MODES:
            return
        old_count = self._view_mode
        if count == 1 and old_count in CAMERA_GRID_MODES:
            self._previous_grid = old_count
        if count == 1:
            if old_count == 0 and not anchor:
                self._tree_camera_selected()
            if anchor:
                self._wall.set_single_camera(anchor)
        self._view_mode = count
        self._mode_buttons.button(count).setChecked(True)
        self._pages.setCurrentIndex(1 if count else 0)
        self._wall_signature = None
        self._refresh_wall(anchor)
        self._sync_preview()
        self._save_wall_state()

    def _refresh_wall(self, anchor: str = "") -> None:
        if not self._view_mode:
            return
        cameras = [r.camera for r in self._manager.records.values() if not r.removed]
        signature = (
            self._view_mode,
            tuple(
                (c.identifier, c.caption, c.ip_address, c.login, c.password)
                for c in cameras
            ),
        )
        if signature != self._wall_signature:
            self._wall_signature = signature
            self._wall.configure(cameras, self._view_mode, anchor)
        for iid, record in self._manager.records.items():
            active = record.worker is not None or record.status in (
                "starting", "recording", "reconnecting", "stopping"
            )
            self._wall.update_recording(
                iid, STATE_TEXT[record.status], active, _recording_elapsed(record)
            )

    def _sync_preview(self) -> None:
        self._previous_page_button.setVisible(bool(self._view_mode))
        self._next_page_button.setVisible(bool(self._view_mode))
        self._previous_page_button.setEnabled(self._wall.can_move(-1))
        self._next_page_button.setEnabled(self._wall.can_move(1))
        self._record_wall_button.setEnabled(
            bool(self._view_mode and self._wall.visible_cameras())
        )
        if self._view_mode and self.isVisible():
            settings = self._svc.get_settings()
            size = tuple(map(int, settings.preview_resolution.split("x")))
            backend = settings.preview_backend
            self._preview.set_cameras(
                self._wall.visible_cameras(), size, backend,
                self._wall.video_outputs(backend),
                settings.preview_transport,
                fps=settings.preview_fps,
                sizes=self._wall.preview_sizes(size),
            )
            self._refresh_wall()
        else:
            self._preview.stop()

    def _focus_camera(self, iid: str) -> None:
        if iid:
            self._set_view_mode(
                self._previous_grid if self._view_mode == 1 else 1, iid
            )

    def _select_wall_camera(self, index: int) -> None:
        if not self._view_mode or not 0 <= index < len(self._wall._tiles):
            return
        tile = self._wall._tiles[index]
        dialog = CameraSelectionDialog(self._wall.available_cameras(index), tile.iid, self)
        try:
            if dialog.exec() != CameraSelectionDialog.DialogCode.Accepted:
                return
            iid = dialog.selected_iid()
            record = self._manager.records.get(iid)
            if (
                record and not record.removed and self._view_mode
                and index < len(self._wall._tiles) and self._wall._tiles[index] is tile
            ):
                self._wall._choose(index, iid)
        finally:
            dialog.deleteLater()

    def _show_wall_popup(self, index: int, pos: QPoint) -> None:
        if not 0 <= index < len(self._wall._tiles):
            return
        tile = self._wall._tiles[index]
        iid = tile.iid
        menu = QMenu(self)
        menu.addAction(
            _icon_from_key("camera"), "Выбрать камеру…",
            lambda: self._select_wall_camera(index),
        )
        record = self._manager.records.get(iid)
        if record is not None and not record.removed:
            active = bool(record.worker or record.intent)
            start = menu.addAction(
                _icon_from_key("menu_camera_record"), "Записать",
                lambda: self._start_recordings([iid]),
            )
            start.setEnabled(not active)
            stop = menu.addAction(
                _icon_from_key("menu_camera_stop"), "Остановить запись",
                lambda: self._manager.stop(iid),
            )
            stop.setEnabled(active)
            menu.addSeparator()
            clear = menu.addAction(
                _icon_from_key("menu_delete"), "Очистить ячейку",
                lambda: self._wall._choose(index, ""),
            )
            clear.setToolTip("Отключить просмотр камеры в этой ячейке")
            item = self._item_map.get(iid)
            if item is not None:
                self._tree.setCurrentItem(item)
                menu.addSeparator()
                menu.addAction(
                    _icon_from_key("menu_camera_view"), "Открыть во внешнем плеере",
                    self._view_camera,
                )
                menu.addAction(_icon_from_key("menu_folder"), "Открыть папку", self._open_folder)
                menu.addAction(
                    _icon_from_key("menu_video_editor"), "Готовые записи в видеоредакторе…",
                    self._open_video_editor,
                )
        menu.exec(pos)
        menu.deleteLater()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_preview()

    def hideEvent(self, event: QHideEvent) -> None:
        self._preview.stop()
        super().hideEvent(event)

    @property
    def cameras(self):
        return {iid: r.camera for iid, r in self._manager.records.items()}

    def _refresh(self):
        records = self._manager.records
        structure = tuple(
            (iid, r.camera.address, r.camera.cabinet, r.camera.camera_name, r.removed)
            for iid, r in records.items()
        )
        if structure != self._structure:
            selected = self._tree.currentItem()
            selected_id = (
                selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
            )
            expanded = {}

            def remember(item):
                expanded[item.data(0, Qt.ItemDataRole.UserRole)] = item.isExpanded()
                for index in range(item.childCount()):
                    remember(item.child(index))

            for index in range(self._tree.topLevelItemCount()):
                remember(self._tree.topLevelItem(index))
            self._tree.clear()
            self._item_map.clear()
            groups = {}
            for iid, record in records.items():
                camera = record.camera
                for key, label, parent in (
                    ((camera.address,), camera.address, self._tree),
                    ((camera.address, camera.cabinet), camera.cabinet, None),
                ):
                    if key not in groups:
                        actual_parent = (
                            parent if parent is not None else groups[(camera.address,)]
                        )
                        node = QTreeWidgetItem(actual_parent, [label])
                        node.setIcon(
                            COL_ADDRESS,
                            _icon_from_key("folder" if len(key) == 1 else "menu_folder"),
                        )
                        font = node.font(COL_ADDRESS)
                        font.setBold(True)
                        node.setFont(COL_ADDRESS, font)
                        node.setData(0, Qt.ItemDataRole.UserRole, key)
                        node.setExpanded(expanded.get(key, True))
                        groups[key] = node
                item = QTreeWidgetItem(groups[(camera.address, camera.cabinet)])
                item.setIcon(COL_CAMERA, _icon_from_key("camera"))
                item.setText(
                    COL_CAMERA,
                    camera.camera_name + (" (удалена)" if record.removed else ""),
                )
                item.setData(0, Qt.ItemDataRole.UserRole, iid)
                self._item_map[iid] = item
                if iid == selected_id:
                    self._tree.setCurrentItem(item)
            self._structure = structure
        for iid, record in records.items():
            item = self._item_map[iid]
            values = {
                COL_ONLINE: "Онлайн" if record.online else "Оффлайн",
                COL_STATUS: STATE_TEXT[record.status],
                COL_TIME: _recording_elapsed(record),
                COL_SIZE: _human_size(record.size) if record.path else "",
            }
            for col, value in values.items():
                if item.text(col) != value:
                    item.setText(col, value)
            item.setToolTip(COL_STATUS, record.error or STATE_TEXT[record.status])
            item.setToolTip(COL_SIZE, record.path)
            color = {
                "recording": "#198754",
                "error": "#dc3545",
                "starting": "#b07800",
                "reconnecting": "#b07800",
                "stopping": "#b07800",
            }.get(record.status)
            item.setForeground(
                COL_STATUS, QColor(color) if color else self.palette().text()
            )
            item.setIcon(COL_STATUS, _state_icon(QColor(color) if color else None))
        total = sum(not r.removed for r in records.values())
        active = sum(r.status == "recording" for r in records.values())
        waiting = sum(
            r.status in ("starting", "reconnecting") for r in records.values()
        )
        self._summary_label.setText(
            f"Камер: {total} • Запись: {active} • Подключение: {waiting}"
        )
        self._refresh_wall()

    def _selected_ids(self):
        item = self._tree.currentItem()
        if not item:
            return []
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str):
            return [key] if key in self._manager.records else []
        return [
            iid
            for iid, record in self._manager.records.items()
            if (record.camera.address, record.camera.cabinet)[: len(key)] == key
        ]

    def _selected_camera(self):
        ids = self._selected_ids()
        return self._manager.records[ids[0]].camera if len(ids) == 1 else None

    def _view_camera(self):
        camera = self._selected_camera()
        if camera is None:
            return
        player = camera.player.strip()
        if player.lower() in ("", "системный", "system", "default"):
            ok = launch_system_player(camera.rtsp_url, camera.caption)
        else:
            exe = resolve_player_executable(player)
            ok = bool(
                exe and launch_player(build_player_cmd(exe, camera), camera.caption)
            )
        if not ok:
            QMessageBox.warning(
                self, "Плеер", "Не удалось открыть плеер. Проверьте путь в настройках"
            )

    def _start_recording(self):
        self._start_recordings(self._selected_ids())

    def _start_wall_recording(self) -> None:
        ids = []
        if self._view_mode:
            for camera in self._wall.visible_cameras():
                record = self._manager.records.get(camera.identifier)
                if record and not record.removed and not record.worker and not record.intent:
                    ids.append(camera.identifier)
        self._start_recordings(ids)

    def _start_recordings(self, ids: list[str]) -> None:
        if not ids:
            return
        errors = []
        for iid in dict.fromkeys(ids):
            try:
                self._manager.start(iid)
            except ValueError as exc:
                errors.append(f"{self._manager.records[iid].camera.caption}: {exc}")
        if errors:
            QMessageBox.warning(self, "Запись", "\n".join(dict.fromkeys(errors)))

    def _stop_recording(self):
        for iid in self._selected_ids():
            self._manager.stop(iid)

    def _open_folder(self):
        ids = self._selected_ids()
        if not ids:
            return
        record = self._manager.records[ids[0]]
        try:
            path = (
                Path(record.path).parent if record.path else record.camera.record_dir()
            )
            if not path.exists() or not _open_in_file_manager(path):
                QMessageBox.warning(self, "Папка", "Каталог записи недоступен")
        except (OSError, ValueError):
            QMessageBox.warning(self, "Папка", "Некорректный каталог записи")

    def _show_popup(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._selection_popup(self._tree.viewport().mapToGlobal(pos))

    def _selection_popup(self, pos: QPoint) -> None:
        item = self._tree.currentItem()
        if item is None:
            return
        menu = QMenu(self)
        if isinstance(item.data(0, Qt.ItemDataRole.UserRole), str):
            menu.addAction(_icon_from_key("menu_camera_view"), "Посмотреть", self._view_camera)
        menu.addAction(_icon_from_key("menu_camera_record"), "Записать", self._start_recording)
        menu.addAction(_icon_from_key("menu_camera_stop"), "Остановить", self._stop_recording)
        menu.addAction(_icon_from_key("menu_folder"), "Открыть папку", self._open_folder)
        menu.addAction(_icon_from_key("menu_video_editor"), "Готовые записи в видеоредакторе…", self._open_video_editor)
        menu.addSeparator()
        menu.addAction(_icon_from_key("menu_camera_stop"), "Остановить все", self._manager.stop_all)
        menu.exec(pos)

    def _open_video_editor(self) -> None:
        from datetime import datetime

        from src.ui.dialogs.tools.video_editor import VideoEditorDialog

        recordings = self._manager.completed_recordings(self._selected_ids())
        if not recordings:
            QMessageBox.information(self, "Видеоредактор", "С момента запуска приложения завершённых записей пока нет. Дождитесь окончания записи: активный файл не открывается. Более ранние записи можно открыть из папки.")
            return
        captions = {}
        for item in recordings:
            started = item.get("started", 0)
            date = datetime.fromtimestamp(started).strftime("%d.%m.%Y %H:%M:%S") if started else ""
            captions[item["file"]] = " · ".join(part for part in (item.get("caption", ""), date, Path(item["file"]).name) if part)
        editor = VideoEditorDialog(self)
        editor.import_paths([item["file"] for item in recordings], captions, recording_started={item["file"]: item.get("started", 0) or 0 for item in recordings})
        editor.finished.connect(editor.deleteLater)
        editor.setModal(False)
        editor.show()

    def _open_settings(self):
        from src.ui.dialogs.tools.camera_settings import CameraSettingsDialog

        if CameraSettingsDialog(self._svc, self).exec() == QDialog.DialogCode.Accepted:
            self._sync_preview()

    def request_shutdown_recordings(self):
        self._preview.stop()
        self._manager.request_shutdown_recordings()

    def has_pending_workers(self):
        return self._manager.has_pending_workers() or self._preview.has_pending_workers()

    def shutdown_all_recordings(self, timeout=8.0):
        self._preview.stop()
        self._manager.shutdown_all_recordings(timeout)
