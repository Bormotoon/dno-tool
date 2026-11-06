"""Операции таймлайна: выделение, правка клипов, диапазон In/Out и переходы курсора."""

from __future__ import annotations


from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
)

from src.domain.video.models import (
    Clip,
    ClipEdit,
    Track,
)

from .preview_widget import PreviewWidget
from .source_monitor import SourceMonitor


class VideoTimelineMixin:
    """Часть VideoEditorDialog: состояние и виджеты принадлежат диалогу."""

    def _seek_boundary(self, direction: int) -> None:
        from src.domain.video.navigation import VideoNavigation

        boundaries = [0.0, self.project.duration()]
        boundaries.extend(
            value
            for track in self.project.tracks
            for clip in track.clips
            for value in (clip.timeline_start, clip.timeline_end)
        )
        self._on_timeline_seek(
            VideoNavigation.next_boundary(boundaries, self._playhead, direction)
        )

    def _active_monitor(self) -> PreviewWidget | SourceMonitor:
        focus = QApplication.focusWidget()
        if self.monitors.mode == "source" or (
            focus is not None and self.source_monitor.isAncestorOf(focus)
        ):
            return self.source_monitor
        return self.preview

    def _mark_active_monitor(self, which: str) -> None:
        if self._active_monitor() is self.source_monitor:
            field = (
                self.source_monitor.source_in
                if which == "in"
                else self.source_monitor.source_out
            )
            field.setValue(self.source_monitor.player.position() / 1000)
        else:
            self._set_mark(which)

    def _seek_active_monitor(self, *, end: bool) -> None:
        if self._active_monitor() is self.source_monitor:
            asset = self.source_monitor.asset
            self.source_monitor.seek(
                round(asset.duration * 1000) if end and asset else 0
            )
        else:
            self._on_timeline_seek(self.project.duration() if end else 0.0)

    def add_asset_to_timeline(
        self, asset_iid: str, track_iid: str, t: float, select: bool = False
    ) -> Clip | None:
        a = self._assets.get(asset_iid)
        if a is None or a.duration <= 0:
            if a is not None:
                self._warn_no_duration(a)
            else:
                QMessageBox.warning(
                    self, "Таймлайн", "Метаданные еще загружаются, подождите секунду."
                )
            return None
        track = next((x for x in self.project.tracks if x.iid == track_iid), None)
        if track is None:
            return None
        # несовпадение типа файла и дорожки: НЕ молча перекидывать,
        # а объяснить - типичная причина "случайных" дублей на V1
        if (a.kind == "video" and track.kind != "video") or (
            a.kind == "audio" and track.kind != "audio"
        ):
            cands = [x for x in self.project.tracks if x.kind == a.kind]
            if not cands:
                QMessageBox.warning(
                    self, "Таймлайн", f"Нет дорожки типа {a.kind} для этого файла."
                )
                return None
            want = a.kind == "video" and "видео" or "аудио"
            have = track.kind == "video" and "видео" or "аудио"
            r = QMessageBox.question(
                self,
                "Таймлайн",
                f"Файл {want}, а дорожка {have}. Положить на {cands[0].name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return None
            track = cands[0]
            t = max([c.timeline_end for c in track.clips], default=0.0)
        before = self._take_snapshot()
        try:
            clip = self.edits.insert_asset(a.iid, track.iid, t)
        except ValueError as error:
            QMessageBox.warning(self, "Таймлайн", str(error))
            return None
        self.push_undo(before, "Добавить клип")
        if select:
            # выделение после rebuild сцены: refresh_all пересоздает items
            self._select_clip(clip.iid, track.iid)
        return clip

    def _selected_clip_obj(self) -> tuple[Clip, Track] | None:
        if not self._sel_clip:
            return None
        for tr in self.project.tracks:
            for c in tr.clips:
                if c.iid == self._sel_clip:
                    return (c, tr)
        return None

    def _select_clip(self, clip_iid: str, track_iid: str) -> None:
        """Выделить клип: внутреннее состояние + визуально на таймлайне."""
        self._finish_edit_sessions()
        self._sel_clip = clip_iid or None
        self._sel_text = None
        self._sel_track = track_iid
        self.timeline.select_clip(clip_iid)
        self._sync_audio_controls()
        self._apply_preview_audio()
        self.inspector_controller.refresh()
        self._update_editor_actions()

    def _on_clip_selected(self, clip_iid: str, track_iid: str) -> None:
        if getattr(self, "_refreshing_selection", False):
            return
        if clip_iid:
            self._select_clip(clip_iid, track_iid)
        elif not self.timeline._selected_track:
            self._finish_edit_sessions()
            self._sel_clip = self._sel_track = None
            self._sync_audio_controls()
            self.inspector_controller.refresh()
            self._update_editor_actions()

    @Slot(str)
    def _on_track_selected(self, iid: str) -> None:
        self._finish_edit_sessions()
        self._sel_clip = self._sel_text = None
        self._sel_track = iid or None
        self.timeline._selected_track = iid or None
        self.timeline.headers.update()
        self._sync_audio_controls()
        self.inspector_controller.refresh()
        self._update_editor_actions()

    @Slot(str)
    def _on_text_selected(self, iid: str) -> None:
        if getattr(self, "_refreshing_selection", False):
            return
        if iid:
            if self._sel_text == iid:
                return
            self._finish_edit_sessions()
            self._sel_clip = self._sel_track = None
            self._sel_text = iid
            self.timeline.select_clip(iid)
            self.inspector_controller.open("text")
            self.text_panel.select_overlay(iid)
        elif self._sel_text:
            self._sel_text = None
            self.inspector_controller.refresh()
        self._update_editor_actions()

    @Slot(str, float)
    def _split_clip(self, iid: str, time: float) -> None:
        self._finish_edit_sessions()
        before = self._take_snapshot()
        try:
            if self.edits.split_clip(iid, time):
                self.push_undo(before, "Разрезать клип и связанный звук")
        except ValueError as error:
            self.notice.show_error(str(error))

    @Slot(str, float, float)
    def _on_text_interval_edited(self, iid: str, start: float, end: float) -> None:
        before = self._take_snapshot()
        try:
            changed = self.edits.text_interval(iid, start, end)
        except ValueError as error:
            self.notice.show_error(str(error))
            self.timeline.refresh()
            return
        if changed:
            self.push_undo(before, "Переместить / ограничить текст")
        else:
            self.timeline.refresh()

    @Slot(str, str)
    def _on_clip_action(self, action: str, iid: str) -> None:
        if any(text.iid == iid for text in self.project.text_overlays):
            self._sel_text, self._sel_clip = iid, None
        else:
            found = next(
                (
                    (clip, track)
                    for track in self.project.tracks
                    for clip in track.clips
                    if clip.iid == iid
                ),
                None,
            )
            if found is None:
                return
            self._select_clip(found[0].iid, found[1].iid)
        if action == "edit_text":
            self.text_panel.select_overlay(iid, focus=True)
        elif action == "unlink":
            self.unlink_selected_clip()
        elif action in ("remove_audio", "remove_video"):
            self.remove_selected_component(
                "audio" if action == "remove_audio" else "video"
            )
        elif action == "ripple":
            self.delete_selected(ripple=True)
        elif action == "delete":
            self.delete_selected()

    def delete_selection(self) -> None:
        """Отдельный блок удаляется без вырезания времени из других дорожек."""
        self.delete_selected(ripple=False)

    def remove_selected_component(self, kind: str) -> None:
        found = self._selected_clip_obj()
        if found is None:
            return
        clip = found[0]
        group = [
            (item.iid, track.iid)
            for track in self.project.tracks
            for item in track.clips
            if item.iid == clip.iid
            or (clip.link_iid and item.link_iid == clip.link_iid)
        ]
        before = self._take_snapshot()
        try:
            changed = self.edits.remove_component(clip.iid, kind)
        except ValueError as error:
            QMessageBox.warning(self, "Видео и звук", str(error))
            return
        if changed:
            self.push_undo(
                before,
                "Удалить только звук" if kind == "audio" else "Удалить только видео",
            )
            for iid, track_iid in group:
                if any(
                    item.iid == iid
                    for track in self.project.tracks
                    for item in track.clips
                ):
                    self._select_clip(iid, track_iid)
                    break

    def split_at_playhead(self) -> None:
        t = self._playhead
        if t < 0:
            return
        before = self._take_snapshot()
        try:
            if self.project.split_at(t):
                self.push_undo(before, "Разрезать")
        except ValueError as error:
            QMessageBox.warning(self, "Разрезать", str(error))

    def delete_selected(self, *, ripple: bool = False) -> None:
        if self._sel_text:
            iid = self._sel_text
            before = self._take_snapshot()
            try:
                self.edits.texts(
                    [text for text in self.project.text_overlays if text.iid != iid]
                )
            except ValueError as error:
                self.notice.show_error(str(error))
                return
            self._sel_text = None
            self.push_undo(before, "Удалить текст")
            return
        found = self._selected_clip_obj()
        if not found:
            return
        before = self._take_snapshot()
        try:
            if not self.project.delete_clips({found[0].iid}, ripple=ripple):
                return
        except ValueError as error:
            QMessageBox.warning(self, "Удаление", str(error))
            return
        self._sel_clip = None
        self.push_undo(
            before, "Удалить со сдвигом" if ripple else "Удалить с промежутком"
        )

    def _set_mark(self, which: str) -> None:
        if which == "in":
            self._mark_in = self._playhead
        else:
            self._mark_out = self._playhead
        mi, mo = self._mark_in, self._mark_out
        self.preview.set_marks_text(mi, mo)
        self.timeline.set_marks(mi, mo, focus=which)
        self._update_editor_actions()

    def cut_marked_range(self) -> None:
        self._edit_marked_range(keep=False)

    def keep_marked_range(self) -> None:
        self._edit_marked_range(keep=True)

    def _edit_marked_range(self, *, keep: bool) -> None:
        if self._mark_in is None or self._mark_out is None:
            QMessageBox.warning(self, "Диапазон", "Поставьте метки I и O.")
            return
        before = self._take_snapshot()
        try:
            changed = self.project.edit_range(self._mark_in, self._mark_out, keep=keep)
        except ValueError as error:
            QMessageBox.warning(self, "Диапазон", str(error))
            return
        self._mark_in = self._mark_out = None
        self.preview.set_marks_text(None, None)
        self.timeline.set_marks(None, None)
        if changed:
            self.push_undo(before, "Оставить фрагмент" if keep else "Вырезать диапазон")

    def _on_timeline_edited(self, edit: ClipEdit) -> None:
        before = self._take_snapshot()
        try:
            changed = self.project.edit_clip(edit)
        except ValueError as error:
            self.notice.show_error(str(error))
            self.timeline.refresh()
            return
        if changed:
            if self._sel_clip == edit.clip_iid:
                self._sel_track = edit.track_iid
            self.push_undo(before, "Переместить / обрезать клип")
        else:
            self.timeline.refresh()

    def _on_timeline_seek(self, t: float) -> None:
        if self._closing:
            return
        self._playhead = t
        self.timeline.set_playhead(t)
        self.preview.seek_timeline(t)
        self._apply_audio_at_playhead()
        self._update_editor_actions()

    def _on_preview_seek(self, t: float) -> None:
        if self._closing:
            return
        self._playhead = t
        self.timeline.set_playhead(t)
        # тянем видео за слайдером превью
        self.preview.seek_timeline(t)
        self._apply_audio_at_playhead()
        self._update_editor_actions()

    def _on_playback_pos(self, t: float) -> None:
        if self._closing:
            return
        # след от воспроизведения: плейхед + громкость клипа под ним,
        # без обратного seek (seek_timeline здесь - петля и рывки звука)
        self._playhead = t
        self.timeline.set_playhead(t)
        self._apply_audio_at_playhead()
        self._update_editor_actions()
