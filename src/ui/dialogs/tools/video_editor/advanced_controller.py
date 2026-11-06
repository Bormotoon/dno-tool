"""Управление слоями, преобразованиями и привязкой титров через доменные операции."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from src.domain.video.models import TextOverlay

if TYPE_CHECKING:
    from .dialog import VideoEditorDialog

T = TypeVar("T")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QInputDialog,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class AdvancedController(QObject):
    def __init__(self, view: VideoEditorDialog) -> None:
        super().__init__(view)
        self.view = view
        self.drag_iid = ""

    def _commit(self, title: str, operation: Callable[[], object]) -> None:
        self.view._finish_edit_sessions()
        before = self.view._take_snapshot()
        try:
            operation()
        except ValueError as error:
            QMessageBox.warning(self.view, title, str(error))
            return
        self.view.push_undo(before, title)

    def _choose(
        self, title: str, objects: list[T], label: Callable[[T], str]
    ) -> T | None:
        if not objects:
            return None
        labels = [f"{index + 1}. {label(item)}" for index, item in enumerate(objects)]
        chosen, accepted = QInputDialog.getItem(
            self.view, title, "Выбор:", labels, 0, False
        )
        return objects[labels.index(chosen)] if accepted else None

    def add_video_track(self) -> None:
        name, accepted = QInputDialog.getText(
            self.view,
            "Видеослой",
            "Название:",
            text=f"V{len(self.view.project.video_tracks()) + 1}",
        )
        if accepted:
            self._commit(
                "Добавить видеослой", lambda: self.view.project.add_track("video", name)
            )

    def remove_track(self) -> None:
        track = next(
            (tr for tr in self.view.project.tracks if tr.iid == self.view._sel_track),
            None,
        )
        if track is not None:
            answer = QMessageBox.question(
                self.view, "Удалить дорожку", f"Удалить «{track.name}» и её клипы?"
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._commit(
                    "Удалить дорожку", lambda: self.view.project.remove_track(track.iid)
                )

    def reorder_track(self) -> None:
        tracks = self.view.project.video_tracks()
        track = next((tr for tr in tracks if tr.iid == self.view._sel_track), None)
        if track is None:
            return
        index, accepted = QInputDialog.getInt(
            self.view,
            "Порядок слоёв",
            "Позиция (1 — нижний слой):",
            tracks.index(track) + 1,
            1,
            len(tracks),
        )
        if accepted:

            def apply() -> None:
                ordered = list(tracks)
                ordered.remove(track)
                ordered.insert(index - 1, track)
                self.view.edits.reorder_video([item.iid for item in ordered])

            self._commit("Порядок видеослоёв", apply)

    @staticmethod
    def _dialog(title: str, parent: QWidget) -> tuple[QDialog, QFormLayout]:
        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        dialog.setSizeGripEnabled(True)
        dialog.resize(500, 540)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        host = QWidget(scroll)
        form = QFormLayout(host)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll.setWidget(host)
        layout.addWidget(scroll)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog, form

    @staticmethod
    def _number(
        form: QFormLayout, title: str, value: float, low: float, high: float
    ) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setDecimals(6)
        widget.setRange(low, high)
        widget.setValue(value)
        widget.setAccessibleName(title)
        form.addRow(title, widget)
        return widget

    def edit_track(self) -> None:
        self.view.inspector_controller.open("track")

    def edit_transform(self) -> None:
        self.view.inspector_controller.open("video")

    def edit_speed(self) -> None:
        self.view.inspector_controller.open("bounds")

    def video_crossfade(self) -> None:
        selected = self.view._selected_clip_obj()
        if selected is None:
            return
        duration, accepted = QInputDialog.getDouble(
            self.view,
            "Видеопереход",
            "Длительность, с (нужны запасы исходников):",
            0.5,
            0.001,
            600,
            3,
        )
        if accepted:
            self._commit(
                "Видеопереход",
                lambda: self.view.project.crossfade_video(selected[0].iid, duration),
            )

    def _title(self) -> TextOverlay | None:
        self.view._finish_edit_sessions()
        return next(
            (
                t
                for t in self.view.project.text_overlays
                if t.iid == self.view._sel_text
            ),
            None,
        )

    def bind_title(self) -> None:
        text = self._title()
        if text is None:
            return
        clips = [
            (c, tr)
            for tr in self.view.project.tracks
            if not tr.locked
            for c in tr.sorted_clips()
        ]
        selected = self._choose(
            "Клип для выбранного титра",
            clips,
            lambda pair: f"{pair[1].name} · {self.view.project.asset_by_iid(pair[0].asset_iid).name} · {pair[0].timeline_start:.3f} с",
        )
        if selected:
            clip = selected[0]
            self._commit(
                "Привязать титр к клипу",
                lambda: self.view.edits.properties(
                    "text",
                    text.iid,
                    {"clip_iid": clip.iid, "start": 0, "end": clip.duration},
                ),
            )

    def safe_title(self) -> None:
        text = self._title()
        if text is not None:
            size = (self.view.preview._video_w, self.view.preview._video_h)
            values = {
                "margin_x": round(size[0] * 0.05),
                "margin_y": round(size[1] * 0.05),
            }
            self._commit(
                "Безопасные отступы титра",
                lambda: self.view.edits.properties("text", text.iid, values),
            )

    def unbind_title(self) -> None:
        text = self._title()
        if text is None or not text.clip_iid:
            return
        clip = next(
            (
                clip
                for track in self.view.project.tracks
                for clip in track.clips
                if clip.iid == text.clip_iid
            ),
            None,
        )
        values = {
            "clip_iid": "",
            "start": (clip.timeline_start if clip else 0) + text.start,
            "end": (
                (clip.timeline_start + min(text.end or clip.duration, clip.duration))
                if clip
                else text.end
            ),
        }
        self._commit(
            "Отвязать титр",
            lambda: self.view.edits.properties("text", text.iid, values),
        )

    def title_preset(self) -> None:
        text = self._title()
        if text is None:
            return
        labels = ["Заголовок слева вверху", "Нижняя треть", "Субтитры справа внизу"]
        label, accepted = QInputDialog.getItem(
            self.view, "Оформление титра", "Пресет:", labels, 0, False
        )
        if accepted:
            width = self.view.preview._video_w or self.view.project.canvas.width or 640
            height = (
                self.view.preview._video_h or self.view.project.canvas.height or 360
            )
            index = labels.index(label)
            values = {
                "corner": ("TL", "BL", "BR")[index],
                "margin_x": round(width * 0.05),
                "margin_y": round(height * 0.05),
                "fontsize": max(8, round(height * (0.06 if index == 0 else 0.04))),
                "back_alpha": 60,
                "color": "white",
                "back_color": "black",
            }
            self._commit(
                "Оформление титра",
                lambda: self.view.edits.properties("text", text.iid, values),
            )

    def drag_title(self) -> None:
        text = self._title()
        if text is None:
            return
        self.drag_iid = text.iid
        self.view.preview.view.begin_title_drag()

    def title_dragged(self, dx: float, dy: float) -> None:
        text = next(
            (
                item
                for item in self.view.project.text_overlays
                if item.iid == self.drag_iid
            ),
            None,
        )
        self.drag_iid = ""
        if text is None:
            return
        width, height = self.view.preview._video_w, self.view.preview._video_h
        values = {
            "margin_x": max(
                0,
                min(
                    16384,
                    round(
                        text.margin_x
                        + dx * width * (-1 if text.corner.endswith("R") else 1)
                    ),
                ),
            ),
            "margin_y": max(
                0,
                min(
                    16384,
                    round(
                        text.margin_y
                        + dy * height * (-1 if text.corner.startswith("B") else 1)
                    ),
                ),
            ),
        }
        self._commit(
            "Переместить титр",
            lambda: self.view.edits.properties("text", text.iid, values),
        )

    def edit_mask(self) -> None:
        from copy import deepcopy

        from src.domain.video.masks import VideoMask

        project = self.view.project
        if not project.duration():
            return
        choices = ["Новая маска"] + [
            f"{index+1}: {item.start:.3f}–{item.end:.3f} с"
            for index, item in enumerate(project.masks)
        ]
        label, accepted = QInputDialog.getItem(
            self.view, "Маски", "Маска:", choices, 0, False
        )
        if not accepted:
            return
        index = choices.index(label) - 1
        mask = (
            deepcopy(project.masks[index])
            if index >= 0
            else VideoMask(
                start=min(self.view._playhead, max(0, project.duration() - 0.04)),
                end=project.duration(),
            )
        )
        dialog, form = self._dialog("Непрозрачная маска области", self.view)
        fields = {}
        for key, title, maximum in (
            ("start", "Начало, с:", project.duration()),
            ("end", "Конец, с:", project.duration()),
            ("x", "X (доля ширины):", 1),
            ("y", "Y (доля высоты):", 1),
            ("width", "Ширина (доля):", 1),
            ("height", "Высота (доля):", 1),
        ):
            fields[key] = self._number(form, title, getattr(mask, key), 0, maximum)
        remove = QCheckBox("Удалить эту маску")
        remove.setEnabled(index >= 0)
        form.addRow(remove)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def apply() -> None:
            for key, field in fields.items():
                setattr(mask, key, field.value())
            masks = deepcopy(project.masks)
            if index >= 0:
                masks.pop(index)
            if not remove.isChecked():
                mask.validate(project.duration())
                masks.append(mask)
            self.view.edits.masks(masks)

        self._commit("Маски областей кадра", apply)

    def storyboard(self) -> None:
        from src.domain.video.render_plan import RenderPlan

        from .storyboard import StoryboardDialog

        self.view._finish_edit_sessions()
        project = self.view.project
        if not project.duration():
            return
        try:
            plan = RenderPlan.from_project(project)
            entries = [
                (marker.name, marker.start)
                for marker in sorted(project.markers, key=lambda item: item.start)
            ]
            if not entries:
                entries = [
                    (f"Клип {index+1}", clip.start)
                    for index, clip in enumerate(plan.video)
                ] or [("Начало", 0)]
            self._storyboard = StoryboardDialog(plan, entries, self.view)
            self._storyboard.seek.connect(self.view._on_timeline_seek)
            self._storyboard.exec()
            self._storyboard.deleteLater()
        except ValueError as error:
            QMessageBox.warning(self.view, "Раскадровка", str(error))

    def template_title(self) -> None:
        from src.domain.video.models import TextOverlay
        from src.domain.video.title_template import TitleTemplate

        selected = self.view._selected_clip_obj()
        if selected is None:
            return
        clip = selected[0]
        asset = self.view.project.asset_by_iid(clip.asset_iid)
        if asset is None:
            return
        template, accepted = QInputDialog.getText(
            self.view,
            "Титр выбранного кадра",
            "Поля: {source}, {timecode}, {recorded_at}. Подпись остаётся редактируемой:",
            text="{source} · {timecode}",
        )
        if not accepted:
            return

        def apply() -> None:
            position = max(
                0, min(clip.duration, self.view._playhead - clip.timeline_start)
            )
            text = TitleTemplate.expand(
                template, asset, clip.src_in + position * clip.speed
            )
            self.view.edits.texts(
                [
                    *self.view.project.text_overlays,
                    TextOverlay(
                        text=text, clip_iid=clip.iid, end=clip.duration, corner="BL"
                    ),
                ]
            )

        self._commit("Титр по шаблону", apply)
