"""Общие QAction для меню, таймлайна, контекста и клавиатуры."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QToolBar,
    QToolButton,
    QWidget,
)

from src.ui.icons import set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS

if TYPE_CHECKING:
    from .dialog import VideoEditorDialog


class EditActions(QObject):
    """Одна операция имеет один обработчик и одну проверку доступности."""

    def __init__(self, editor: VideoEditorDialog) -> None:
        super().__init__(editor)
        self.editor = editor
        self.actions: dict[str, QAction] = {}
        self._shortcuts: dict[str, list[str]] = {}
        self._typing = False
        self._tips: dict[str, str] = {}
        self._add(
            "undo",
            "Отменить",
            editor.undo_edit,
            ["Ctrl+Z"],
            "ve_undo",
            editor._undo_action,
        )
        self._add(
            "redo",
            "Повторить",
            editor.redo_edit,
            ["Ctrl+Y", "Ctrl+Shift+Z"],
            "ve_redo",
            editor._redo_action,
        )
        self._add(
            "select",
            "Выделение",
            lambda: self.set_tool("select"),
            ["A", "Ф", "Escape"],
            "ve_select",
        )
        self._add(
            "blade", "Лезвие", lambda: self.set_tool("blade"), ["B", "И"], "ve_blade"
        )
        self.tools = QActionGroup(self)
        for key in ("select", "blade"):
            self.actions[key].setCheckable(True)
            self.tools.addAction(self.actions[key])
        self.actions["select"].setChecked(True)
        self._add(
            "split",
            "Разрезать все дорожки по курсору",
            editor.split_at_playhead,
            ["S", "Ы"],
            "ve_split",
        )
        self._add(
            "delete",
            "Удалить с промежутком",
            lambda: editor.delete_selected(ripple=False),
            ["Delete"],
            "ve_delete",
        )
        self._add(
            "ripple",
            "Удалить со сдвигом",
            lambda: editor.delete_selected(ripple=True),
            ["Shift+Delete"],
            "ve_ripple_delete",
        )
        self._add("snap", "Привязка", self.toggle_snap, ["N", "Т"], "ve_snap")
        self.actions["snap"].setCheckable(True)
        self.actions["snap"].setChecked(True)
        self._add(
            "cut_range",
            "Вырезать In/Out на всех дорожках",
            editor.cut_marked_range,
            icon="ve_cut_range",
        )
        self._add(
            "keep_range",
            "Оставить In/Out на всех дорожках",
            editor.keep_marked_range,
            icon="ve_keep_range",
        )
        self._add(
            "bounds",
            "Границы и скорость…",
            lambda: editor.inspector_controller.open("bounds"),
        )
        self._add(
            "video",
            "Видео: преобразования и кадрирование…",
            lambda: editor.inspector_controller.open("video"),
        )
        self._add(
            "rotate_right",
            "Повернуть на 90° по часовой стрелке",
            lambda: editor.inspector_controller.rotate(90),
            ["R", "К"],
        )
        self._add(
            "rotate_left",
            "Повернуть на 90° против часовой стрелки",
            lambda: editor.inspector_controller.rotate(-90),
            ["Shift+R", "Shift+К"],
        )
        self._add(
            "flip_h",
            "Отразить по горизонтали",
            lambda: editor.inspector_controller.flip("flip_h"),
            ["H", "Р"],
        )
        self._add(
            "flip_v",
            "Отразить по вертикали",
            lambda: editor.inspector_controller.flip("flip_v"),
            ["V", "М"],
        )
        self._add(
            "audio", "Звук клипа…", lambda: editor.inspector_controller.open("audio")
        )
        self._add(
            "normalize", "Нормализовать пик до −1 dBFS…", editor.normalize_clip_audio
        )
        self._add(
            "video_transition",
            "Видеопереход со следующим клипом…",
            editor._advanced.video_crossfade,
        )
        self._add(
            "audio_transition",
            "Кроссфейд со следующим звуковым клипом…",
            editor.crossfade_selected_audio,
        )
        self._add(
            "unlink",
            "Разделить видео и аудио",
            editor.unlink_selected_clip,
            ["Ctrl+Shift+U"],
        )
        self._add(
            "remove_audio",
            "Удалить только звук",
            lambda: editor.remove_selected_component("audio"),
        )
        self._add(
            "remove_video",
            "Удалить только видео",
            lambda: editor.remove_selected_component("video"),
        )
        self._add(
            "track",
            "Свойства дорожки…",
            lambda: editor.inspector_controller.open("track"),
        )
        self._add("track_order", "Порядок видеослоёв…", editor._advanced.reorder_track)
        self._add("track_delete", "Удалить дорожку…", editor._advanced.remove_track)
        self._add("add_audio", "Добавить аудиодорожку…", editor.add_audio_track)
        self._add("add_video", "Добавить видеослой…", editor._advanced.add_video_track)
        self._add(
            "edit_text",
            "Изменить наложение…",
            lambda: editor.inspector_controller.open("text"),
        )
        for key, title, callback in (
            (
                "bind_title",
                "Привязать выбранный титр к клипу…",
                editor._advanced.bind_title,
            ),
            ("unbind_title", "Отвязать выбранный титр", editor._advanced.unbind_title),
            (
                "title_preset",
                "Оформление выбранного титра…",
                editor._advanced.title_preset,
            ),
            (
                "drag_title",
                "Переместить титр в предпросмотре…",
                editor._advanced.drag_title,
            ),
            (
                "safe_title",
                "Безопасные отступы титра (5%)",
                editor._advanced.safe_title,
            ),
            (
                "template_title",
                "Титр по шаблону выбранного кадра…",
                editor._advanced.template_title,
            ),
            ("mask", "Маски результата монтажа…", editor._advanced.edit_mask),
        ):
            self._add(key, title, callback)
        editor._clip_properties_action = self.actions["bounds"]
        editor._unlink_action = self.actions["unlink"]
        editor._component_actions = (
            (self.actions["remove_audio"], "audio"),
            (self.actions["remove_video"], "video"),
        )

    def _add(
        self,
        key: str,
        title: str,
        callback: Callable[[], Any],
        shortcuts: list[str] | None = None,
        icon: str | None = None,
        action: QAction | None = None,
    ) -> None:
        action = action or QAction(title, self.editor)
        if key not in ("undo", "redo"):
            action.triggered.connect(lambda checked=False: self.invoke(key, callback))
        self.editor.addAction(action)
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        if icon:
            set_themed_icon(action, icon, toggle=key in ("select", "blade", "snap"))
        self.actions[key] = action
        action.setData(key)
        self._shortcuts[key] = shortcuts or []
        self._tips[key] = title + (f" ({', '.join(shortcuts)})" if shortcuts else "")
        action.setToolTip(self._tips[key])
        action.setShortcuts([QKeySequence(seq) for seq in shortcuts or []])
        action.setIconText(
            {
                "undo": "Отмена",
                "redo": "Повтор",
                "split": "Разрез: все дорожки",
                "delete": "С промежутком",
                "ripple": "Со сдвигом",
            }.get(key, title)
        )

    def invoke(self, key: str, callback: Callable[[], Any]) -> None:
        self.editor._finish_edit_sessions()
        self.update()
        if self.actions[key].isEnabled():
            callback()
        self.update()

    def typing(self, typing: bool) -> None:
        if self._typing == typing:
            return
        self._typing = typing
        for key, sequences in self._shortcuts.items():
            self.actions[key].setShortcuts(
                [] if typing else [QKeySequence(seq) for seq in sequences]
            )

    def set_tool(self, tool: str) -> None:
        self.editor.timeline.set_tool(tool)
        self.actions[tool].setChecked(True)

    def toggle_snap(self) -> None:
        self.editor.timeline.snapping.setChecked(self.actions["snap"].isChecked())

    def build_menu(self, menu: QMenu) -> None:
        menu.setToolTipsVisible(True)

        def group(target: QMenu, keys: tuple[str, ...]) -> None:
            for key in keys:
                target.addAction(self.actions[key])

        group(menu, ("undo", "redo"))
        menu.addSeparator()
        group(menu, ("select", "blade", "split", "delete", "ripple", "snap"))
        menu.addSeparator()
        group(
            menu, ("cut_range", "keep_range", "unlink", "remove_audio", "remove_video")
        )
        for label, keys in (
            (
                "Клип",
                (
                    "bounds",
                    "video",
                    "rotate_right",
                    "rotate_left",
                    "flip_h",
                    "flip_v",
                    "audio",
                    "normalize",
                    "video_transition",
                    "audio_transition",
                ),
            ),
            (
                "Дорожки",
                ("track", "track_order", "track_delete", "add_audio", "add_video"),
            ),
            (
                "Титры",
                (
                    "edit_text",
                    "bind_title",
                    "unbind_title",
                    "title_preset",
                    "drag_title",
                    "safe_title",
                    "template_title",
                ),
            ),
            ("Маски — результат монтажа", ("mask",)),
        ):
            group(menu.addMenu(label), keys)

    def build_toolbar(self) -> None:
        timeline = self.editor.timeline
        preview = self.editor.preview
        self.bar = QWidget(timeline)
        bar_layout = QHBoxLayout(self.bar)
        bar_layout.setContentsMargins(
            timeline.headers.width() + timeline.view.frameWidth(), 0, 0, 0
        )
        bar_layout.setSpacing(8)
        toolbar = QToolBar("Монтаж", timeline)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.setIconSize(QSize(32, 32))
        toolbar.setStyleSheet(
            "QToolBar { border: none; background: transparent; padding: 0; }"
            + FLAT_TOOLBUTTON_QSS
        )
        for keys in (
            ("undo", "redo"),
            ("select", "blade"),
            ("split", "delete", "ripple"),
            ("snap",),
        ):
            if toolbar.actions():
                toolbar.addSeparator()
            for key in keys:
                toolbar.addAction(self.actions[key])
        self.toolbar = toolbar
        ranges = QToolBar("Диапазон", timeline)
        ranges.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        ranges.setIconSize(QSize(32, 32))
        ranges.setStyleSheet(toolbar.styleSheet())
        for button in (preview.btn_mark_in, preview.btn_mark_out):
            ranges.addWidget(button)
        ranges.addSeparator()
        for key in ("cut_range", "keep_range"):
            ranges.addAction(self.actions[key])
        ranges.addSeparator()
        timeline.zoom.setMinimumWidth(64)
        timeline.zoom.setMaximumWidth(96)
        timeline.zoom.setToolTip("Масштаб таймлайна; Ctrl+колесо — масштаб у курсора")
        timeline.zoom.setAccessibleName("Масштаб таймлайна")
        ranges.addWidget(timeline.zoom)
        ranges.addWidget(timeline.fit_project_button)
        ranges.addWidget(timeline.fit_selection_button)
        timeline.range_toolbar = ranges
        self.transport = QWidget(preview.controls)
        transport_layout = QHBoxLayout(self.transport)
        transport_layout.setContentsMargins(0, 0, 0, 0)
        transport_layout.setSpacing(4)
        for button in (
            preview.btn_start,
            preview.btn_play,
            preview.btn_pause,
            preview.btn_stop,
            preview.btn_prev,
            preview.btn_next,
        ):
            transport_layout.addWidget(button)
        # Старые монтажные кнопки заменяются единственными кнопками общих QAction.
        for attribute, key, owner in (
            ("btn_split", "split", toolbar),
            ("btn_del", "delete", toolbar),
            ("btn_cut_range", "cut_range", ranges),
            ("btn_keep_range", "keep_range", ranges),
        ):
            old = getattr(preview, attribute)
            preview.button_row.removeWidget(old)
            old.hide()
            old.deleteLater()
            setattr(preview, attribute, owner.widgetForAction(self.actions[key]))
        while preview.button_row.count():
            preview.button_row.takeAt(0)
        preview.button_row.addStretch(1)
        preview.button_row.addWidget(self.transport)
        preview.button_row.addStretch(1)
        timeline.zoom_controls.hide()
        timeline.layout().removeWidget(timeline.zoom_controls)
        for group in (toolbar, ranges):
            for button in group.findChildren(QToolButton):
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                button.setIconSize(QSize(32, 32))
                button.setFixedSize(32, 32)
                button.setAutoRaise(True)
                if button.defaultAction():
                    button.setAccessibleName(button.defaultAction().text())
        bar_layout.addWidget(toolbar)
        bar_layout.addStretch(1)
        bar_layout.addWidget(ranges)
        timeline.layout().insertWidget(0, self.bar)
        timeline.snapping.hide()
        timeline.snapping.toggled.connect(self.actions["snap"].setChecked)

    def _enable(self, key: str, enabled: bool, reason: str = "") -> None:
        action = self.actions[key]
        action.setEnabled(enabled and not self.editor._closing)
        action.setToolTip(
            self._tips[key] + (f"\n{reason}" if not enabled and reason else "")
        )
        action.setStatusTip(action.toolTip())

    def update(self) -> None:
        e = self.editor
        found = e._selected_clip_obj()
        pair = [
            (tr, c)
            for tr in e.project.tracks
            for c in tr.clips
            if found
            and (
                c.iid == found[0].iid
                or (found[0].link_iid and c.link_iid == found[0].link_iid)
            )
        ]
        locked = any(tr.locked for tr, _ in pair)
        title = next((t for t in e.project.text_overlays if t.iid == e._sel_text), None)
        title_locked = bool(
            title
            and any(
                tr.locked and any(c.iid == title.clip_iid for c in tr.clips)
                for tr in e.project.tracks
            )
        )
        reason = "Дорожка заблокирована" if locked or title_locked else "Нет выделения"
        for key in ("bounds", "video", "audio", "track", "edit_text"):
            self._enable(
                key,
                (
                    bool(found)
                    if key == "bounds"
                    else (
                        any(tr.kind == "video" for tr, _ in pair)
                        if key == "video"
                        else (
                            any(tr.kind == "audio" for tr, _ in pair)
                            if key == "audio"
                            else (
                                bool(e._sel_track)
                                if key == "track"
                                else title is not None
                            )
                        )
                    )
                ),
                "Нет подходящего выделения",
            )
        for key in ("rotate_right", "rotate_left", "flip_h", "flip_v"):
            self._enable(
                key,
                any(tr.kind == "video" for tr, _ in pair) and not locked,
                reason if locked else "Выберите клип с видео",
            )
        self._enable(
            "delete", bool(found or title) and not (locked or title_locked), reason
        )
        ripple_locked = any(tr.locked for tr in e.project.tracks)
        self._enable(
            "ripple",
            bool(found) and not ripple_locked,
            (
                "Дорожка затронутая сдвигом заблокирована"
                if ripple_locked
                else "Выберите клип; титры удаляются с промежутком"
            ),
        )
        for key, kind in (("remove_audio", "audio"), ("remove_video", "video")):
            self._enable(
                key, not locked and any(tr.kind == kind for tr, _ in pair), reason
            )
        self._enable(
            "unlink",
            bool(found and found[0].link_iid) and not locked,
            reason if locked or not found else "Клип не связан со звуком",
        )
        self._enable(
            "normalize",
            not locked
            and any(tr.kind == "audio" for tr, _ in pair)
            and e._audio_analysis is None,
            (
                "Дорожка заблокирована"
                if locked
                else "Нет связанного звука или выполняется анализ"
            ),
        )
        for key, kind in (("video_transition", "video"), ("audio_transition", "audio")):
            target = next(((tr, c) for tr, c in pair if tr.kind == kind), None)
            candidates = (
                target[0].clips
                if target and kind == "video"
                else [c for tr in e.project.audio_tracks() for c in tr.clips]
            )
            following = (
                next(
                    (
                        c
                        for c in candidates
                        if c.iid != target[1].iid
                        and abs(c.timeline_start - target[1].timeline_end) < 1e-6
                    ),
                    None,
                )
                if target
                else None
            )
            transition_locked = locked or bool(
                following
                and any(
                    tr.locked
                    and any(
                        c.iid == following.iid
                        or (following.link_iid and c.link_iid == following.link_iid)
                        for c in tr.clips
                    )
                    for tr in e.project.tracks
                )
            )
            asset = e.project.asset_by_iid(target[1].asset_iid) if target else None
            handles = bool(
                target
                and following
                and asset
                and asset.duration > target[1].src_out + 1e-6
                and following.src_in > 1e-6
            )
            self._enable(
                key,
                handles and not transition_locked,
                (
                    "Дорожка заблокирована"
                    if transition_locked
                    else "Нет подходящего следующего клипа или запаса исходников для перехода"
                ),
            )
        split_targets = [
            (tr, c)
            for tr in e.project.tracks
            for c in tr.clips
            if c.timeline_start < e._playhead < c.timeline_end
        ]
        self._enable(
            "split",
            bool(split_targets) and not any(tr.locked for tr, _ in split_targets),
            (
                "Дорожка заблокирована"
                if any(tr.locked for tr, _ in split_targets)
                else "Нет клипов под курсором"
            ),
        )
        valid_range = (
            e._mark_in is not None
            and e._mark_out is not None
            and 0 <= e._mark_in < e._mark_out <= e.project.duration()
        )
        for key in ("cut_range", "keep_range"):
            self._enable(
                key,
                valid_range and not any(tr.locked for tr in e.project.tracks),
                (
                    "Дорожка заблокирована"
                    if any(tr.locked for tr in e.project.tracks)
                    else "Задайте диапазон In/Out"
                ),
            )
        track = next((tr for tr in e.project.tracks if tr.iid == e._sel_track), None)
        track_links = (
            {c.link_iid for c in track.clips if c.link_iid} if track else set()
        )
        linked_track_locked = any(
            tr.locked and any(c.link_iid in track_links for c in tr.clips)
            for tr in e.project.tracks
        )
        self._enable(
            "track_order",
            bool(track and track.kind == "video" and not track.locked),
            "Выберите незаблокированный видеослой",
        )
        self._enable(
            "track_delete",
            bool(
                track
                and not track.locked
                and not linked_track_locked
                and sum(tr.kind == track.kind for tr in e.project.tracks) > 1
            ),
            "Выберите дорожку; последнюю дорожку типа удалить нельзя",
        )
        for key in (
            "bind_title",
            "unbind_title",
            "title_preset",
            "drag_title",
            "safe_title",
        ):
            self._enable(
                key,
                title is not None
                and not title_locked
                and (key != "unbind_title" or bool(title.clip_iid)),
                reason,
            )
        self._enable("template_title", bool(found) and not locked, reason)
        self._enable("mask", e.project.duration() > 0, "Проект пуст")
        # Иконки под просмотром остаются на месте и отражают общую доступность.
        for button, key in (
            (e.preview.btn_split, "split"),
            (e.preview.btn_del, "delete"),
            (e.preview.btn_cut_range, "cut_range"),
            (e.preview.btn_keep_range, "keep_range"),
        ):
            button.setEnabled(self.actions[key].isEnabled())
            button.setToolTip(self.actions[key].toolTip())

    def context_menu(self, kind: str) -> QMenu:
        self.update()
        menu = QMenu(self.editor)
        menu.setToolTipsVisible(True)
        keys = (
            ("track", "track_order", "track_delete")
            if kind == "track"
            else (
                ("edit_text", "bind_title", "unbind_title", "title_preset", "delete")
                if kind == "text"
                else (
                    "bounds",
                    "video",
                    "rotate_right",
                    "rotate_left",
                    "flip_h",
                    "flip_v",
                    "audio",
                    "video_transition",
                    "audio_transition",
                    "unlink",
                    "remove_audio",
                    "remove_video",
                    "delete",
                    "ripple",
                )
            )
        )
        for key in keys:
            if kind == "audio" and key == "video_transition":
                continue
            menu.addAction(self.actions[key])
        return menu
